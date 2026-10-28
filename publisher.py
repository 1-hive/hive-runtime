#!/usr/bin/env python3
"""Publish reviewed work: push each result whose review passed from the hive's copy of its repository to the
repository's own home (e.g. GitHub), and say where to open the pull request.

    publisher.py --registry REGISTRY.json [--keys DIR] [--say CMD] [--deny REGEX ...] [--init-only] [--dry-run]

A repository whose registry entry lists a hive copy first (a bare repository on the hive's host, a file:// URL)
and its own home after it is *staged*: agents' results go to the hive copy (the sidecar pushes them to
refs/heads/hive/<task> there), the reviewer reviews them there, and nothing reaches the home until a review
passes. Then this pushes that exact commit to the home's refs/heads/hive/<task> (hive/<task>-r2, -r3... when
that name already holds another commit, e.g. an agent's own push: never forced); a human opens and merges the
pull request. The record's pins stay valid, since the commit is the same in both places.

Each run, for every staged repository:
- makes sure the hive copy exists (bare, deletions and rewrites refused, git/pre-receive installed), and mirrors
  the home's branches into it, except hive/*, which belong to the hive;
- for each passed review of a result with a code pin there that isn't published yet (the first run only records
  the results passed before it): scans what the result adds
  (base..code) for secrets and --deny patterns, and on a match publishes nothing and alerts; otherwise pushes.

Keys: the push to a home uses DIR/<repository> (e.g. a deploy key with write access to that repository only),
else DIR/default; an https home is pushed over ssh (git@host:owner/name.git). Without a key the repository is
only mirrored, and an alert says so once. Alerts are printed, and with --say each is also passed to CMD as its
last argument (e.g. hive-bridge's telegram-say.py); a published result's also with HIVE_TASK=<task> in its
environment, so the PR link can go where the task was asked for. Reads the record as the supervisor; state in
WORK_DIR/.publisher.json. Run it from a timer, e.g. every 2 minutes.
"""
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import hive_paths as hp

STATE = hp.WORK_DIR / ".publisher.json"
HERE = Path(__file__).resolve().parent
SECRETS = re.compile(r"(sk-[A-Za-z0-9_-]{20,}|sk-ant-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
                     r"|xox[abpr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
                     r"|\b[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}\b|AIza[0-9A-Za-z_-]{35})")


def events(etype: str) -> list[dict]:
    env = hp.tool_env(hp.CONFIG_DIR / "agents" / "supervisor.key", "publisher")
    out = subprocess.run(["hive", "events", "--type", etype], capture_output=True, text=True, env=env, check=True).stdout
    return [json.loads(x) for x in out.splitlines() if x.strip()]


def passed_results() -> list[dict]:
    """Each result whose review passed: {task, repository, code, base}, oldest first."""
    passed = {(e.get("data") or {}).get("result_event") for e in events("review.recorded")
              if (e.get("data") or {}).get("verdict") == "passed"}
    out = []
    for e in events("task.result_posted"):
        if e["event_id"] not in passed:
            continue
        pins: dict[str, dict] = {}
        for r in e.get("refs") or []:
            pin = r.get("pin") or {}
            if r.get("rel") in ("code", "base") and pin.get("commit_oid"):
                pins.setdefault(pin["repository"], {})[r["rel"]] = pin["commit_oid"].split(":", 1)[-1]
        for repo, p in pins.items():
            if "code" in p:
                out.append({"task": e.get("task"), "repository": repo, "code": p["code"], "base": p.get("base")})
    return out


def staged(registry: dict) -> dict[str, tuple[Path, str]]:
    """repository -> (its hive copy, its home): entries whose first fetch URL is a file:// hive copy."""
    out = {}
    for name, r in registry["repositories"].items():
        urls = r.get("fetch_urls") or []
        if len(urls) >= 2 and urls[0].startswith("file://"):
            out[name] = (Path(urls[0][len("file://"):]), urls[1])
    return out


def git(repo: Path | None, *args: str, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["git", *(["-C", str(repo)] if repo else []), *args]
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=600, check=False)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args[:2])}: {(r.stderr or r.stdout).strip()[-300:]}")
    return r


def ensure_copy(copy: Path) -> None:
    """The hive copy, bare: agents may only add to hive/* (pre-receive, over hive-git-shell), and no push may
    delete or rewrite a ref, so no pinned commit becomes unreachable."""
    if not (copy / "HEAD").is_file():
        copy.parent.mkdir(parents=True, exist_ok=True)
        git(None, "init", "-q", "--bare", "--shared=group", str(copy))
    for k, v in (("receive.denyDeletes", "true"), ("receive.denyNonFastForwards", "true"),
                 ("core.sharedRepository", "group")):
        git(copy, "config", k, v)
    hook = copy / "hooks" / "pre-receive"
    if not hook.is_file() or hook.read_bytes() != (HERE / "git" / "pre-receive").read_bytes():
        shutil.copy2(HERE / "git" / "pre-receive", hook)
        hook.chmod(0o755)


def mirror(copy: Path, home: str) -> None:
    """The home's branches, except hive/* (the hive's own), and its default branch as HEAD."""
    git(copy, "fetch", "-q", "--no-tags", home, "+refs/heads/*:refs/heads/*", "^refs/heads/hive/*")
    head = git(None, "ls-remote", "--symref", home, "HEAD", check=False).stdout
    m = re.search(r"^ref: (refs/heads/\S+)\s+HEAD", head, re.MULTILINE)
    if m:
        git(copy, "symbolic-ref", "HEAD", m.group(1))


def push_url(home: str) -> str:
    """Push over ssh what is fetched over https: https://host/owner/name(.git) -> git@host:owner/name.git."""
    m = re.match(r"^https://([^/]+)/([^/]+)/([^/]+?)(\.git)?/?$", home)
    return f"git@{m.group(1)}:{m.group(2)}/{m.group(3)}.git" if m else home


def key_for(keys: Path | None, repo: str) -> Path | None:
    for name in (repo, "default"):
        if keys and (keys / name).is_file():
            return keys / name
    return None


def scan(copy: Path, base: str | None, code: str, deny: list[re.Pattern]) -> list[str]:
    """Lines the result adds (base..code) that look like a secret or match a --deny pattern."""
    diff = (git(copy, "diff", "--no-color", base, code) if base
            else git(copy, "show", "--format=", "--no-color", code)).stdout
    hits = []
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            for p in [SECRETS, *deny]:
                m = p.search(line)
                if m:
                    hits.append(m.group(0)[:12] + "…")
    return hits


def pr_branch(copy: Path, base: str | None) -> str | None:
    """The home branch the result should merge into: the default branch if it holds the base, else the first that does."""
    default = git(copy, "symbolic-ref", "--short", "HEAD", check=False).stdout.strip()
    branches = [b for b in git(copy, "for-each-ref", "--format=%(refname:short)", "refs/heads").stdout.split()
                if not b.startswith("hive/")]
    for b in ([default] if default in branches else []) + [b for b in branches if b != default]:
        if not base or git(copy, "merge-base", "--is-ancestor", base, b, check=False).returncode == 0:
            return b
    return None


def compare_link(home: str, branch: str | None, head: str) -> str:
    m = re.match(r"^https://(github\.com)/([^/]+)/([^/]+?)(\.git)?/?$", home)
    if m and branch:
        return f"https://github.com/{m.group(2)}/{m.group(3)}/compare/{branch}...{head}?expand=1"
    return f"{home} branch {head}" + (f" into {branch}" if branch else "")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--registry", required=True)
    ap.add_argument("--keys", help="folder of push keys: <repository> or default")
    ap.add_argument("--say", help="command that sends an alert, given as its last argument")
    ap.add_argument("--deny", action="append", default=[], metavar="REGEX",
                    help="also refuse to publish a result adding a line that matches (repeatable)")
    ap.add_argument("--init-only", action="store_true", help="create and mirror the hive copies, publish nothing")
    ap.add_argument("--dry-run", action="store_true", help="say what would be published, push nothing")
    a = ap.parse_args()
    reg = json.loads(Path(a.registry).read_text())
    repos = staged(reg)
    keys = Path(a.keys) if a.keys else None
    deny = [re.compile(d) for d in a.deny]
    state = json.loads(STATE.read_text()) if STATE.is_file() else {}
    first = "published" not in state
    done, warned = state.setdefault("published", {}), state.setdefault("warned", {})

    def alert(key: str, msg: str, task: str | None = None) -> None:
        if warned.get(key) == msg:
            return
        warned[key] = msg
        print(msg)
        if a.say:
            env = {**os.environ, "HIVE_TASK": task} if task else None
            subprocess.run([*shlex.split(a.say), msg], check=False, env=env)

    for name, (copy, home) in sorted(repos.items()):
        try:
            ensure_copy(copy)
            mirror(copy, home)
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            print(f"{name}: {e}", file=sys.stderr)
    if a.init_only:
        return                                           # copies only: the record isn't read, the state untouched
    if first and not a.dry_run:
        # The first run only records what review had passed before: those results reached their homes the old way.
        for r in passed_results():
            done[f"{r['task']}:{r['repository']}"] = r["code"]
    else:
        for r in passed_results():
            name, task, code = r["repository"], r["task"], r["code"]
            key = f"{task}:{name}"
            if name not in repos or done.get(key) == code:
                continue
            copy, home = repos[name]
            # The branch in its home: hive/<task>, or hive/<task>-r2, -r3... when that name already holds something
            # else (an agent's own push that skipped the hive, an earlier result): never forced, never overwritten.
            names = [f"hive/{task}"] + [f"hive/{task}-r{n}" for n in range(2, 21)]
            there = {}
            for line in git(None, "ls-remote", home, f"refs/heads/hive/{task}*", check=False).stdout.splitlines():
                sha, ref = line.split()
                if ref.removeprefix("refs/heads/") in names:
                    there[ref.removeprefix("refs/heads/")] = sha
            if code in there.values():                  # already in its home (pushed there before the hive staged it)
                done[key] = code
                continue
            head = next((n for n in names if n not in there), None)
            if not head:
                alert(key, f"publisher: {task}'s branch names in {name} (hive/{task} to -r20) are all taken: not published")
                continue
            if git(copy, "cat-file", "-e", f"{code}^{{commit}}", check=False).returncode:
                alert(key, f"publisher: {task}'s reviewed commit {code[:10]} isn't in the hive copy of {name}: not published")
                continue
            hits = scan(copy, r["base"], code, deny)
            if hits:
                alert(key, f"publisher: NOT publishing {task} ({name} {code[:10]}): what it adds looks like a secret or "
                           f"a denied pattern ({', '.join(sorted(set(hits))[:5])}). Check it; nothing left the hive.")
                continue
            k = key_for(keys, name)
            if not k:
                alert(f"nokey:{name}", f"publisher: no push key for {name} (keys/{name} or keys/default): its reviewed "
                                       f"results stay in the hive copy")
                continue
            if a.dry_run:
                print(f"would publish {name} {code[:10]} to {push_url(home)} {head}")
                continue
            env = {"GIT_SSH_COMMAND": f"ssh -i {shlex.quote(str(k))} -o IdentitiesOnly=yes -o BatchMode=yes "
                                      "-o StrictHostKeyChecking=accept-new", "GIT_TERMINAL_PROMPT": "0",
                   "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(Path.home())}
            p = git(copy, "push", "-q", push_url(home), f"{code}:refs/heads/{head}", env=env, check=False)
            if p.returncode:
                alert(key, f"publisher: pushing {task} ({name} {code[:10]}) to {head} failed: "
                           f"{(p.stderr or p.stdout).strip()[-200:]}")
                continue
            done[key] = code
            warned.pop(key, None)
            taken = f" ({there[f'hive/{task}'][:10]} was already on hive/{task}: left as it is)" \
                if head != f"hive/{task}" else ""
            alert(key, f"Reviewed and published: {task} → {name} {head} ({code[:10]}){taken}. "
                       f"Open the pull request: {compare_link(home, pr_branch(copy, r['base']), head)}", task)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1))


if __name__ == "__main__":
    main()
