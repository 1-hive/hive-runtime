#!/usr/bin/env python3
"""Merge a goal's reviewed code into main, on the operator's word.

    merge.py plan <goal> [--key KEY] [--json]       what merging would do: each repository's main, the reviewed commits
                                                     that would go in (fast-forward or merge commit), what's skipped and
                                                     why, any conflict; a digest names this exact plan
    merge.py run <goal> --digest D [--key KEY] [--via WHO]
                                                     do it, if the plan is still the one with digest D (the one the
                                                     operator saw); prints the outcome as JSON

Agents never push a code repository's main (git/pre-receive): "merging is decided after review", and the operator
merged by hand. Now the operator's Accept + merge tap (hive-bridge), or `op merge`, decides and this does the git work,
so nobody runs git. Run as the operator (the host user that owns the bare repositories), never by an agent.

What gets merged:
- The goal's `hive-merge` block, if its summary (the `summary` ref of goal.completed) has one: a repository and a commit
  (7+ hex) per line, in order, optionally with a note; or the single line `none`. Each commit must be the `code` ref of
  a task result whose review passed: any goal's, so a summary can carry an earlier goal's script-only branches.
- Otherwise, every task of the goal: its current result's `code` refs, if that result's latest review passed.
- Skipped: a commit already in main; one contained in another that will be merged; a repository whose bare clone has
  a remote `origin` (a mirror: its home is upstream, so merging is done there, e.g. a GitHub pull request), or that is
  a staged hive copy (the registry gives a home after it, as on proto-hive: publisher.py opens the pull request
  there); one not in the registry as a local bare repository; the workspace.
- Into the branch the bare repository's HEAD names (main), in order: a fast-forward when main is behind the commit,
  else a merge commit (`git merge-tree`, no worktree) by `operator` that names the goal, task, result and review.
  Either way every new non-merge commit on main is a reviewed one, so main-watch.py passes it.
- A conflict is found when planning: that repository merges nothing, and the plan says which files. Nothing is merged
  anywhere while any repository has one; a merge task (a worker resolving it on a hive/ branch, reviewed) is the way.
- main moves with a compare-and-swap (`git update-ref NEW OLD`): if it moved since the plan, nothing there changes.
Each run is logged to WORK_DIR/.merges.jsonl.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import hive_paths as hp

BLOCK = re.compile(r"^```hive-merge[ \t]*\n(.*?)^```[ \t]*$", re.M | re.S)
LOG = hp.WORK_DIR / ".merges.jsonl"
LOCK = hp.WORK_DIR / ".merges.lock"
MERGEABLE = ("completed", "accepted")


class Refused(Exception):
    pass


# ---- the record --------------------------------------------------------------------------------------------------

def hive(key: str, *args: str) -> str:
    r = subprocess.run(["hive", *args], env=hp.tool_env(key), capture_output=True, text=True)
    if r.returncode:
        raise Refused(f"hive {' '.join(args[:2])}: {(r.stderr or r.stdout).strip()[-300:]}")
    return r.stdout


def events(key: str, etype: str) -> list[dict]:
    return [json.loads(x) for x in hive(key, "events", "--type", etype).splitlines() if x.strip()]


class Record:
    """What the plan needs from the record: the goal, its tasks, and which results passed review."""

    def __init__(self, key: str):
        state = json.loads(hive(key, "state"))
        self.goals, self.tasks = state["goals"], state["tasks"]
        self.results = {e["event_id"]: e for e in events(key, "task.result_posted")}
        last: dict[str, dict] = {}
        for e in sorted(events(key, "review.recorded"), key=lambda e: e["position"]):
            rid = (e.get("data") or {}).get("result_event")
            if rid:
                last[rid] = e
        self.review_of = last   # result event id -> its latest review

    def passed(self, result_id: str) -> dict | None:
        r = self.review_of.get(result_id)
        return r if r and (r.get("data") or {}).get("verdict") == "passed" else None

    def code_refs(self, result: dict) -> list[tuple[str, str]]:
        return [(r["pin"]["repository"], str(r["pin"]["commit_oid"]).split(":", 1)[-1])
                for r in result.get("refs") or [] if r.get("rel") == "code" and (r.get("pin") or {}).get("commit_oid")]

    def reviewed(self) -> dict[tuple[str, str], dict]:
        """(repository, commit) -> where it comes from, for every result whose latest review passed."""
        out = {}
        for rid, res in sorted(self.results.items(), key=lambda kv: kv[1]["position"]):
            rev = self.passed(rid)
            if rev:
                for repo, oid in self.code_refs(res):
                    out.setdefault((repo, oid), origin(res, rev))
        return out


def origin(res: dict, rev: dict) -> dict:
    return {"task": res.get("task"), "result_at": res["position"], "review_at": rev["position"],
            "reviewer": (rev.get("actor") or {}).get("id")}


def read_pin(pin: dict) -> str | None:
    if (pin or {}).get("repository") != "workspace" or not pin.get("path"):
        return None
    oid = str(pin["commit_oid"]).split(":", 1)[-1]
    r = subprocess.run(["git", "-C", str(hp.WORKSPACE_REPO), "show", f"{oid}:{pin['path']}"],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def parse_block(text: str) -> list[tuple[str, str, str]] | None:
    """The summary's hive-merge block: [(repository, commit prefix, note)], [] for `none`, None if there's no block."""
    blocks = BLOCK.findall(text or "")
    if not blocks:
        if re.search(r"^```hive-merge", text or "", re.M):
            raise Refused("the summary's hive-merge block isn't closed")
        return None
    if len(blocks) > 1:
        raise Refused("the summary has more than one hive-merge block")
    out = []
    for line in blocks[0].splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower() == "none":
            return []
        parts = line.split(None, 2)
        if len(parts) < 2 or not re.fullmatch(r"[0-9a-f]{7,64}", parts[1]):
            raise Refused(f"hive-merge line {line[:80]!r}: expected `<repository> <commit, 7+ hex> [note]`")
        out.append((parts[0], parts[1], parts[2] if len(parts) > 2 else ""))
    return out


# ---- git ---------------------------------------------------------------------------------------------------------

def git(repo: Path, *args: str, check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                       env={**os.environ, **(env or {})})
    if check and r.returncode:
        raise Refused(f"git {' '.join(args[:2])} in {repo.name}: {r.stderr.strip()[-300:]}")
    return r


def ancestor(repo: Path, a: str, b: str) -> bool:
    return git(repo, "merge-base", "--is-ancestor", a, b, check=False).returncode == 0


def bare_clone(name: str) -> tuple[Path | None, str | None]:
    """The repository's local bare clone, or why there's none to merge into."""
    try:
        reg = json.loads((hp.DEPLOY_DIR / "registry.json").read_text())["repositories"]
    except (OSError, ValueError, KeyError):
        return None, "the registry can't be read"
    r = reg.get(name)
    if not r:
        return None, "not in the registry"
    url = (r.get("fetch_urls") or [""])[0]
    if not url.startswith("file://"):
        return None, f"its home is {urlparse(url).netloc or url}: merge there"
    path = Path(urlparse(url).path)
    if path.resolve() == hp.WORKSPACE_REPO.resolve():
        return None, "the workspace (agents push its main themselves)"
    if git(path, "rev-parse", "--is-bare-repository", check=False).stdout.strip() != "true":
        return None, f"{path} isn't a bare repository"
    # A staged hive copy (publisher.py): a second fetch URL is its home, which gets reviewed commits as pull requests,
    # and the copy's own branches are mirrored from there, so a merge here would never reach the home.
    home = next((u for u in (r.get("fetch_urls") or [])[1:] if not u.startswith("file://")), None)
    if home:   # read (is the code already in?), never written
        return path, f"its home is {urlparse(home).netloc or home}: merge its pull request there"
    up = git(path, "config", "--get", "remote.origin.url", check=False).stdout.strip()
    if up:
        return path, f"a mirror of {urlparse(up).netloc or up}: merge upstream"
    return path, None


def target(repo: Path) -> str:
    return git(repo, "symbolic-ref", "--short", "HEAD", check=False).stdout.strip() or "main"


def label(repo: Path, task: str | None, oid: str) -> str:
    """hive/<task> if that branch points at the commit, else the short commit."""
    if task and git(repo, "rev-parse", "-q", "--verify", f"refs/heads/hive/{task}^{{commit}}",
                    check=False).stdout.strip() == oid:
        return f"hive/{task}"
    return oid[:10]


def merge_commit(repo: Path, cur: str, oid: str, message: str, when: str | None = None) -> tuple[str | None, list[str]]:
    """Merge oid into cur without a worktree: the new commit, or (None, conflicting files)."""
    r = git(repo, "merge-tree", "--write-tree", "--name-only", "--no-messages", cur, oid, check=False)
    lines = r.stdout.splitlines()
    if r.returncode == 1:
        return None, lines[1:] or ["(unknown)"]
    if r.returncode or not lines:
        raise Refused(f"git merge-tree in {repo.name}: {r.stderr.strip()[-300:]}")
    who = {"GIT_AUTHOR_NAME": "operator", "GIT_AUTHOR_EMAIL": f"operator@{hp.HIVE_ID}.invalid",
           "GIT_COMMITTER_NAME": "operator", "GIT_COMMITTER_EMAIL": f"operator@{hp.HIVE_ID}.invalid"}
    if when:
        who.update(GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
    c = git(repo, "commit-tree", lines[0], "-p", cur, "-p", oid, "-m", message, env=who)
    return c.stdout.strip(), []


# ---- the plan ----------------------------------------------------------------------------------------------------

def plan(goal: str, key: str) -> dict:
    rec = Record(key)
    g = rec.goals.get(goal)
    if not g:
        raise Refused(f"no goal {goal}")
    reviewed = rec.reviewed()
    summary = read_pin(g.get("summary") or {}) if g.get("summary") else None
    block = parse_block(summary) if summary else None
    wanted: list[dict] = []   # in order: repo, commit, origin, note
    problems: list[str] = []
    if block is not None:
        source = "the summary's hive-merge block"
        for repo, prefix, note in block:
            hits = [(r, o) for (r, o) in reviewed if r == repo and o.startswith(prefix)]
            if len(hits) != 1:
                problems.append(f"{repo} {prefix}: " + ("not a reviewed commit on the record" if not hits
                                                         else "matches more than one reviewed commit"))
                continue
            wanted.append({"repo": repo, "commit": hits[0][1], **reviewed[hits[0]], "note": note})
    else:
        source = "the goal's reviewed results"
        tasks = sorted((t for t in rec.tasks.values() if t.get("goal") == goal and t.get("status") != "cancelled"),
                       key=lambda t: ((t.get("current_result") or {}).get("position") or 0, t["id"]))
        for t in tasks:
            cur = (t.get("current_result") or {}).get("event_id")
            res = rec.results.get(cur) if cur else None
            if not res or not rec.code_refs(res):
                continue
            rev = rec.passed(cur)
            if not rev:
                problems.append(f"task {t['id']}: its current result has no passed review")
                continue
            for repo, oid in rec.code_refs(res):
                wanted.append({"repo": repo, "commit": oid, **origin(res, rev), "note": ""})
    repos: dict[str, dict] = {}
    for w in wanted:
        r = repos.get(w["repo"])
        if r is None:
            path, why = bare_clone(w["repo"])
            r = repos[w["repo"]] = {"repo": w["repo"], "path": str(path) if path else None, "skipped": why,
                                    "items": []}
            if path:
                r["branch"] = target(path)
                r["head"] = git(path, "rev-parse", f"refs/heads/{r['branch']}").stdout.strip()
        r["items"].append(dict(w))
    for r in repos.values():
        if r["skipped"]:
            for it in r["items"]:
                it["action"] = "skip"
                if r["path"] and ancestor(Path(r["path"]), it["commit"], r["head"]):
                    it["why"] = f"already in {r['branch']}"
            r["merges"] = 0
            continue
        path, head = Path(r["path"]), r["head"]
        items = r["items"]
        for it in items:
            if git(path, "cat-file", "-e", f"{it['commit']}^{{commit}}", check=False).returncode:
                it["action"], it["why"] = "missing", "the commit isn't in this repository"
            elif ancestor(path, it["commit"], head):
                it["action"], it["why"] = "skip", f"already in {r['branch']}"
            it["label"] = label(path, it.get("task"), it["commit"])
        live = [it for it in items if "action" not in it]
        for it in live:
            by = next((o for o in live if o is not it and o["commit"] != it["commit"]
                       and ancestor(path, it["commit"], o["commit"])), None)
            if by:
                it["action"], it["why"] = "skip", f"contained in {by['label']}"
        seen = set()
        cur = head
        for it in items:
            if "action" in it:
                continue
            if it["commit"] in seen:
                it["action"], it["why"] = "skip", "listed twice"
                continue
            seen.add(it["commit"])
            if ancestor(path, cur, it["commit"]):
                it["action"] = "fast-forward"
                cur = it["commit"]
                continue
            new, conflicts = merge_commit(path, cur, it["commit"], "plan (not used)", "1700000000 +0000")
            if new is None:
                it["action"], it["conflicts"] = "conflict", conflicts[:20]
                problems.append(f"{r['repo']}: merging {it['label']} into {r['branch']} conflicts in "
                                + ", ".join(conflicts[:5]) + (" …" if len(conflicts) > 5 else ""))
                break
            it["action"] = "merge"
            cur = new
        for it in items:
            it.setdefault("action", "after-conflict")
        r["merges"] = sum(it["action"] in ("fast-forward", "merge") for it in items)
    out = {"goal": goal, "title": g.get("title"), "status": g.get("status"), "source": source,
           "repos": list(repos.values()), "problems": problems}
    out["mergeable"] = (g.get("status") in MERGEABLE and not problems
                        and any(r.get("merges") for r in repos.values()))
    out["nothing"] = not any(r.get("merges") for r in repos.values())
    out["digest"] = digest(out)
    out["lines"] = describe(out)
    return out


def digest(p: dict) -> str:
    core = [[r["repo"], r.get("head"), [[it["commit"], it["action"]] for it in r["items"]]] for r in p["repos"]]
    return hashlib.sha256(json.dumps([p["goal"], core, p["problems"]]).encode()).hexdigest()[:12]


def describe(p: dict) -> list[str]:
    """The plan in lines a person reads (the bridge's card and `op merge`)."""
    lines = []
    for r in p["repos"]:
        if all(it.get("why", "").startswith("already in") for it in r["items"]):
            continue   # nothing new there
        if r.get("skipped"):
            lines.append(f"{r['repo']}: not merged here ({r['skipped']})")
            continue
        todo = [it for it in r["items"] if it["action"] in ("fast-forward", "merge", "conflict")]
        if not todo:
            lines.append(f"{r['repo']}: nothing new for {r['branch']} "
                         f"({'; '.join(sorted({it.get('why', it['action']) for it in r['items']}))})")
            continue
        for it in todo:
            what = {"fast-forward": "fast-forward to", "merge": "merge", "conflict": "CONFLICT merging"}[it["action"]]
            src = f" (task {it['task']})" if it.get("task") and it["label"] != f"hive/{it['task']}" else ""
            lines.append(f"{r['repo']} {r['branch']}: {what} {it['label']} {it['commit'][:7]}{src}"
                         + (f": {it['note']}" if it.get("note") else ""))
        rest = [it for it in r["items"] if it["action"] == "skip" and "contained" in it.get("why", "")]
        if rest:
            lines.append(f"  (includes {', '.join(it['label'] for it in rest)})")
    return lines


# ---- running it --------------------------------------------------------------------------------------------------

def run(goal: str, key: str, want: str, via: str) -> dict:
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        p = plan(goal, key)
        if p["digest"] != want:
            raise Refused(f"the plan changed since {want} (now {p['digest']}): look again")
        if p["problems"]:
            raise Refused("; ".join(p["problems"]))
        if p["status"] not in MERGEABLE:
            raise Refused(f"goal {goal} is {p['status']}: only a completed or accepted goal's code is merged")
        done = []
        for r in p["repos"]:
            if not r.get("merges"):
                continue
            path, old = Path(r["path"]), r["head"]
            cur, steps = old, []
            for it in r["items"]:
                if it["action"] == "fast-forward":
                    cur = it["commit"]
                elif it["action"] == "merge":
                    msg = (f"Merge {it['label']} ({it['commit'][:10]}) into {r['branch']}\n\n"
                           f"Goal {goal}: {p.get('title') or ''}\n"
                           f"Merged on the operator's word ({via}). Task {it.get('task')}: result at position "
                           f"{it.get('result_at')}, review passed at position {it.get('review_at')}"
                           f" ({it.get('reviewer')}).\n")
                    cur, conflicts = merge_commit(path, cur, it["commit"], msg)
                    if cur is None:
                        raise Refused(f"{r['repo']}: conflict in {', '.join(conflicts[:5])}")
                else:
                    continue
                steps.append({"commit": it["commit"], "label": it["label"], "action": it["action"], "to": cur})
            u = git(path, "update-ref", "-m", f"merge.py: goal {goal} ({via})", f"refs/heads/{r['branch']}", cur, old,
                    check=False)
            ok = u.returncode == 0
            done.append({"repo": r["repo"], "branch": r["branch"], "from": old, "to": cur if ok else old,
                         "ok": ok, "steps": steps, **({} if ok else {"error": u.stderr.strip()[-200:]})})
        out = {"goal": goal, "digest": want, "via": via, "at": dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="seconds"), "ok": all(d["ok"] for d in done) and bool(done), "merged": done}
        out["line"] = outcome_line(out)
        with open(LOG, "a") as f:
            f.write(json.dumps(out) + "\n")
        return out


def outcome_line(o: dict) -> str:
    """One line for the chat and the acceptance note."""
    parts = []
    for d in o.get("merged", []):
        if d["ok"]:
            parts.append(f"{d['repo']} {d['branch']} {d['from'][:7]}→{d['to'][:7]} ("
                         + ", ".join(s["label"] for s in d["steps"]) + ")")
        else:
            parts.append(f"{d['repo']}: not merged ({d.get('error', 'main moved')})")
    return "; ".join(parts) or "nothing to merge"


# ---- commands ----------------------------------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "run"):
        s = sub.add_parser(name)
        s.add_argument("goal")
        s.add_argument("--key", default=os.environ.get("HIVE_KEY_FILE") or str(hp.CONFIG_DIR / "operator.key"))
        if name == "plan":
            s.add_argument("--json", action="store_true")
        else:
            s.add_argument("--digest", required=True)
            s.add_argument("--via", default="op merge")
    a = ap.parse_args()
    try:
        if a.cmd == "plan":
            p = plan(a.goal, a.key)
            if a.json:
                print(json.dumps(p, indent=1))
                return 0
            print(f"Merging goal {a.goal} ({p['status']}), from {p['source']}:")
            for line in describe(p) or ["(no code results)"]:
                print("  " + line)
            for pr in p["problems"]:
                print(f"  problem: {pr}")
            print(f"plan {p['digest']}: " + ("ready to merge" if p["mergeable"] else "nothing to merge" if p["nothing"]
                                             else "can't merge as it is"))
            return 0
        o = run(a.goal, a.key, a.digest, a.via)
        print(json.dumps(o, indent=1))
        return 0 if o["ok"] else 1
    except Refused as e:
        print(json.dumps({"ok": False, "refused": str(e)}) if a.cmd == "run" or getattr(a, "json", False)
              else f"refused: {e}", file=sys.stdout if a.cmd == "run" else sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
