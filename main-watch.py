#!/usr/bin/env python3
"""Flag commits that reach a repository's main branch without a passed review on the record.

    main-watch.py [--say CMD] NAME=BARE_CLONE[:BRANCH] ...
    main-watch.py [--say CMD] --registry REGISTRY.json --under DIR

The second form watches every repository in a hivepin registry whose local_path (a bare clone) is under
DIR, on its default branch (the clone's HEAD).

Where agents could push to main but by agreement don't (they push a branch, open a pull request, and a
human merges it), this is the check after the fact instead of a block. Each run fetches each bare clone
from its origin and compares the branch with the head it saw last time. The new commits are fine if each
non-merge one is part of a result whose current review passed (its `code` ref on the record), so a
merge commit or a fast-forward of a reviewed result passes; a squash or rebase merge makes new commits
and is flagged. The first run only records the heads.

Alerts are printed, and with --say each is also passed to CMD as its last argument (e.g. hive-bridge's
telegram-say.py). Reads the record as the supervisor (hive_paths: CONFIG_DIR/agents/supervisor.key);
state in WORK_DIR/.main-watch.json. Run it from a timer, e.g. every 15 minutes.
"""
import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path

import hive_paths as hp

STATE = hp.WORK_DIR / ".main-watch.json"
ENV = hp.tool_env(hp.CONFIG_DIR / "agents" / "supervisor.key", "main-watch")


def events(etype: str) -> list[dict]:
    out = subprocess.run(["hive", "events", "--type", etype], capture_output=True, text=True, env=ENV, check=True).stdout
    return [json.loads(x) for x in out.splitlines() if x.strip()]


def reviewed_commits() -> dict[str, set[str]]:
    """repository -> code commits of results whose review passed."""
    passed = {(e.get("data") or {}).get("result_event") for e in events("review.recorded")
              if (e.get("data") or {}).get("verdict") == "passed"}
    out: dict[str, set[str]] = {}
    for e in events("task.result_posted"):
        if e["event_id"] not in passed:
            continue
        for r in e.get("refs") or []:
            pin = r.get("pin") or {}
            if r.get("rel") == "code" and pin.get("commit_oid"):
                out.setdefault(pin["repository"], set()).add(pin["commit_oid"].split(":", 1)[-1])
    return out


def git(repo: str, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args[:2])} in {repo}: {r.stderr.strip()}")
    return r.stdout.strip()


def check(name: str, repo: str, branch: str, old: str | None, reviewed: set[str]) -> tuple[str, str | None]:
    git(repo, "fetch", "-q", "origin", f"+refs/heads/{branch}:refs/heads/{branch}")
    new = git(repo, "rev-parse", f"refs/heads/{branch}")
    if not old or old == new:
        return new, None
    if subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", old, new], capture_output=True).returncode:
        return new, f"{name}: {branch} was rewritten ({old[:10]} → {new[:10]}): history before it was replaced."
    covered: set[str] = set()
    for c in reviewed:
        if subprocess.run(["git", "-C", repo, "cat-file", "-e", f"{c}^{{commit}}"], capture_output=True).returncode == 0:
            covered.update(git(repo, "rev-list", c, f"^{old}").split())
    loose = [c for c in git(repo, "rev-list", "--no-merges", f"{old}..{new}").split() if c not in covered]
    if not loose:
        return new, None
    lines = [git(repo, "log", "-1", "--format=%h %an: %s", c)[:120] for c in loose[:5]]
    more = f"\n…and {len(loose) - 5} more" if len(loose) > 5 else ""
    return new, (f"{name}: {len(loose)} commit(s) reached {branch} without a passed review on the record "
                 f"({old[:10]} → {new[:10]}):\n" + "\n".join(lines) + more)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--say", help="command that sends an alert, given as its last argument")
    ap.add_argument("--registry", help="watch the registry's repositories whose local_path is under --under")
    ap.add_argument("--under", help="with --registry: the folder of the bare clones")
    ap.add_argument("repos", nargs="*", metavar="NAME=BARE_CLONE[:BRANCH]")
    a = ap.parse_args()
    if a.registry:
        under = Path(a.under or "/").resolve()
        for name, r in sorted(json.loads(Path(a.registry).read_text())["repositories"].items()):
            path = Path(r.get("local_path") or "/nonexistent").resolve()
            if under in path.parents and (path / "HEAD").is_file():
                head = git(str(path), "symbolic-ref", "--short", "HEAD", check=False) or "main"
                a.repos.append(f"{name}={path}:{head}")
    if not a.repos:
        ap.error("no repositories to watch")
    state = json.loads(STATE.read_text()) if STATE.is_file() else {}
    reviewed = reviewed_commits()
    for spec in a.repos:
        name, _, rest = spec.partition("=")
        repo, _, branch = rest.partition(":")
        branch = branch or "main"
        key = f"{name}:{branch}"
        try:
            state[key], alert = check(name, repo, branch, state.get(key), reviewed.get(name, set()))
        except (RuntimeError, subprocess.CalledProcessError) as e:
            print(f"{key}: {e}", file=sys.stderr)
            continue
        if alert:
            print(alert)
            if a.say:
                subprocess.run([*shlex.split(a.say), alert])
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1))


if __name__ == "__main__":
    main()
