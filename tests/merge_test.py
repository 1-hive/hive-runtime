#!/usr/bin/env python3
"""Merging on the operator's word (merge.py): the Accept + merge tap (hive-bridge) and `op merge` merge a goal's
reviewed code into main, so the operator no longer runs git by hand (2026-11-16).

Checks: the plan (fast-forward, merge commit, already in main, contained in another, a result without a passed
review, a mirror left alone, a conflict found before anything is merged); the summary's hive-merge block (exact list,
`none`, a commit that isn't reviewed); running it (exactly the plan with that digest; main moves by compare-and-swap;
every new non-merge commit on main is a reviewed one; the merge commit names the goal; the log); refusals (a changed
plan, a conflict, a goal that isn't completed or accepted).

    python3 tests/merge_test.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

T = Path(os.path.realpath(tempfile.mkdtemp()))
os.environ.update(HOME=str(T / "home"), HIVE_WORK_DIR=str(T / "work"), HIVE_CONFIG_DIR=str(T / "config"),
                  HIVE_REPOS_DIR=str(T / "repos"), HIVE_DEPLOY_DIR=str(T / "deploy"),
                  HIVE_WORKSPACE_REPO=str(T / "repos" / "workspace.git"), HIVE_ID="test-hive",
                  GIT_CONFIG_GLOBAL=str(T / "gitconfig"), GIT_CONFIG_NOSYSTEM="1")
(T / "home").mkdir()
(T / "gitconfig").write_text("[user]\n\tname = t\n\temail = t@t\n[init]\n\tdefaultBranch = main\n")
HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("merge", HERE / "merge.py")
M = importlib.util.module_from_spec(spec)
sys.modules["merge"] = M
spec.loader.exec_module(M)


def sh(*args, cwd=None):
    r = subprocess.run(list(args), cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout.strip()


def commit(work: Path, path: str, text: str, msg: str) -> str:
    (work / path).write_text(text)
    sh("git", "-C", str(work), "add", path)
    sh("git", "-C", str(work), "commit", "-qm", msg)
    return sh("git", "-C", str(work), "rev-parse", "HEAD")


# A local bare repository `proj` with main and four hive branches, a mirror, and the workspace with two summaries.
repos = T / "repos"
repos.mkdir()
sh("git", "init", "-q", "--bare", str(repos / "proj.git"))
w = T / "w"
sh("git", "clone", "-q", str(repos / "proj.git"), str(w))
c0 = commit(w, "a.txt", "base\n", "base")
commit(w, "b.txt", "b\n", "b")
c0 = sh("git", "-C", str(w), "rev-parse", "HEAD")
sh("git", "-C", str(w), "push", "-q", "origin", "main")
sh("git", "-C", str(w), "checkout", "-qb", "hive/t1")
a1 = commit(w, "a.txt", "base\nt1\n", "t1")
sh("git", "-C", str(w), "checkout", "-qb", "hive/t1b")
a2 = commit(w, "c.txt", "t1b\n", "t1b on t1")
sh("git", "-C", str(w), "checkout", "-q", "-b", "hive/t2", c0)
b1 = commit(w, "b.txt", "b\nt2\n", "t2")
sh("git", "-C", str(w), "checkout", "-q", "-b", "hive/t3", c0)
x1 = commit(w, "a.txt", "base\nt3 differs\n", "t3 conflicts with t1")
sh("git", "-C", str(w), "checkout", "-q", "-b", "hive/t4", c0)
u1 = commit(w, "d.txt", "unreviewed\n", "t4, review failed")
sh("git", "-C", str(w), "push", "-q", "origin", "hive/t1", "hive/t1b", "hive/t2", "hive/t3", "hive/t4")

sh("git", "init", "-q", "--bare", str(repos / "mirror.git"))
sh("git", "-C", str(repos / "mirror.git"), "config", "remote.origin.url", "https://github.com/someone/mirror.git")
mw = T / "mw"
sh("git", "clone", "-q", str(repos / "mirror.git"), str(mw))
m0 = commit(mw, "m.txt", "m\n", "mirror base")
sh("git", "-C", str(mw), "push", "-q", "origin", "main")
sh("git", "-C", str(mw), "checkout", "-qb", "hive/t2")
m1 = commit(mw, "m.txt", "m\nt2\n", "t2 in the mirror")
sh("git", "-C", str(mw), "push", "-q", "origin", "hive/t2")

sh("git", "init", "-q", "--bare", str(repos / "workspace.git"))
ww = T / "ww"
sh("git", "clone", "-q", str(repos / "workspace.git"), str(ww))
(ww / "s").mkdir()
s_block = commit(ww, "s/block.md", f"# Summary\n\n```hive-merge\nproj {b1[:9]}  t2 only\n```\n", "block summary")
s_none = commit(ww, "s/none.md", "# Summary\n\n```hive-merge\nnone\n```\n", "none summary")
s_bad = commit(ww, "s/bad.md", f"```hive-merge\nproj {u1[:8]}\n```\n", "unreviewed in block")
sh("git", "-C", str(ww), "push", "-q", "origin", "main")

(T / "deploy").mkdir()
(T / "deploy" / "registry.json").write_text(json.dumps({"version": 1, "repositories": {
    "proj": {"fetch_urls": [f"file://{repos / 'proj.git'}"]},
    "mirror": {"fetch_urls": [f"file://{repos / 'mirror.git'}"]},
    "workspace": {"fetch_urls": [f"file://{repos / 'workspace.git'}"]},
    "gh": {"fetch_urls": ["https://github.com/1-hive/gh.git"]}}}))


# The record, as merge.Record would read it.
def result(rid, task, pos, refs):
    return {"event_id": rid, "task": task, "position": pos,
            "refs": [{"rel": "code", "pin": {"repository": r, "commit_oid": f"sha1:{o}"}} for r, o in refs]}


RESULTS = {
    "r1": result("r1", "t1", 10, [("proj", a1)]),
    "r1b": result("r1b", "t1b", 11, [("proj", a2)]),
    "r2": result("r2", "t2", 12, [("proj", b1), ("mirror", m1), ("gh", "f" * 40)]),
    "r3": result("r3", "t3", 13, [("proj", x1)]),
    "r4": result("r4", "t4", 14, [("proj", u1)]),
    "r5": result("r5", "t5", 15, [("mirror", m0)]),
}
VERDICT = {"r1": "passed", "r1b": "passed", "r2": "passed", "r3": "passed", "r4": "failed", "r5": "passed"}
TASKS = {t: {"id": t, "goal": g, "status": "done", "current_result": {"event_id": r, "position": RESULTS[r]["position"]}}
         for t, g, r in [("t1", "g1", "r1"), ("t2", "g1", "r2"), ("t3", "g2", "r3"), ("t1x", "g2", "r1"),
                         ("t1b", "g3", "r1b"), ("t1y", "g3", "r1"), ("t4", "g4", "r4"), ("t5", "g5", "r5")]}
GOALS = {g: {"id": g, "title": f"goal {g}", "status": "completed"} for g in ("g1", "g2", "g3", "g4", "g5")}
GOALS["active"] = {"id": "active", "status": "active", "title": "not done"}
TASKS["ta"] = {"id": "ta", "goal": "active", "status": "done", "current_result": {"event_id": "r2", "position": 12}}
for g, c, path in [("gblock", s_block, "s/block.md"), ("gnone", s_none, "s/none.md"), ("gbad", s_bad, "s/bad.md")]:
    GOALS[g] = {"id": g, "status": "accepted", "title": g,
                "summary": {"repository": "workspace", "commit_oid": f"sha1:{c}", "path": path}}
    TASKS[f"t-{g}"] = {"id": f"t-{g}", "goal": g, "status": "done", "current_result": {"event_id": "r1", "position": 10}}


class FakeRecord(M.Record):
    def __init__(self, key):
        self.goals, self.tasks, self.results = GOALS, TASKS, RESULTS
        self.review_of = {rid: {"position": RESULTS[rid]["position"] + 100, "actor": {"id": "reviewer.x"},
                                "data": {"verdict": v, "result_event": rid}} for rid, v in VERDICT.items()}


M.Record = FakeRecord
P = repos / "proj.git"


def main_of(repo=P):
    return sh("git", "-C", str(repo), "rev-parse", "refs/heads/main")


def actions(p, repo="proj"):
    r = next(r for r in p["repos"] if r["repo"] == repo)
    return [(it.get("task"), it["action"]) for it in r["items"]]


# g1: t1 fast-forwards, t2 needs a merge commit; the mirror and the GitHub repo are left alone.
p = M.plan("g1", "k")
assert p["mergeable"] and not p["problems"], p
assert actions(p) == [("t1", "fast-forward"), ("t2", "merge")], actions(p)
mirror = next(r for r in p["repos"] if r["repo"] == "mirror")
assert mirror["skipped"].startswith("a mirror of github.com") and mirror["merges"] == 0, mirror
gh = next(r for r in p["repos"] if r["repo"] == "gh")
assert gh["skipped"].startswith("its home is github.com") and gh["path"] is None, gh
text = "\n".join(M.describe(p))
assert "proj main: fast-forward to hive/t1" in text and "proj main: merge hive/t2" in text, text
assert "mirror: not merged here (a mirror of github.com: merge upstream)" in text, text
assert "gh: not merged here (its home is github.com: merge there)" in text, text
assert main_of() == c0, "planning changes nothing"

# The digest names the plan: another one is refused.
try:
    M.run("g1", "k", "000000000000", "test")
    raise AssertionError("ran with a stale digest")
except M.Refused as e:
    assert "plan changed" in str(e), e
assert main_of() == c0

o = M.run("g1", "k", p["digest"], "test tap")
assert o["ok"], o
new = main_of()
assert new != c0 and o["merged"][0]["from"] == c0 and o["merged"][0]["to"] == new, o
assert sh("git", "-C", str(P), "rev-list", "--no-merges", f"{c0}..{new}").split() == [b1, a1] or \
    set(sh("git", "-C", str(P), "rev-list", "--no-merges", f"{c0}..{new}").split()) == {a1, b1}, \
    "only reviewed commits are new on main (main-watch's rule)"
parents = sh("git", "-C", str(P), "log", "-1", "--format=%P", new).split()
assert parents == [a1, b1], parents
info = sh("git", "-C", str(P), "log", "-1", "--format=%an <%ae>%n%B", new)
assert info.startswith("operator <operator@test-hive.invalid>") and "Goal g1: goal g1" in info \
    and "Merge hive/t2" in info and "test tap" in info and "review passed at position 112" in info, info
log = [json.loads(x) for x in (T / "work" / ".merges.jsonl").read_text().splitlines()]
assert log[-1]["goal"] == "g1" and log[-1]["ok"] and log[-1]["merged"][0]["to"] == new, log
assert M.outcome_line(o) == f"proj main {c0[:7]}→{new[:7]} (hive/t1, hive/t2)", M.outcome_line(o)
assert main_of(repos / "mirror.git") == m0, "a mirror is never written"

# Again: nothing left.
p = M.plan("g1", "k")
assert p["nothing"] and not p["mergeable"] and actions(p) == [("t1", "skip"), ("t2", "skip")], p
assert "proj" not in "\n".join(M.describe(p)), M.describe(p)

# g2: t3 conflicts with t1 (now in main): found when planning, nothing merged.
p = M.plan("g2", "k")
assert not p["mergeable"] and p["problems"] and "conflicts in a.txt" in p["problems"][0], p
assert ("t3", "conflict") in actions(p), actions(p)
assert "CONFLICT merging hive/t3" in "\n".join(M.describe(p)), M.describe(p)
try:
    M.run("g2", "k", p["digest"], "test")
    raise AssertionError("merged with a conflict")
except M.Refused as e:
    assert "conflicts" in str(e), e
assert main_of() == new

# g3: t1's result (already in main) and t1b, which builds on it: a merge commit, as main has t2's merge.
p = M.plan("g3", "k")
assert actions(p) == [("t1", "skip"), ("t1b", "merge")], actions(p)

# A result whose latest review failed is a problem, not merged.
p = M.plan("g4", "k")
assert not p["mergeable"] and "t4: its current result has no passed review" in p["problems"][0], p

# Contained: before t1 is in main, t1 inside t1b is skipped as contained.
sh("git", "-C", str(P), "update-ref", "refs/heads/main", c0, new)
p = M.plan("g3", "k")
assert actions(p) == [("t1", "skip"), ("t1b", "fast-forward")], actions(p)
assert next(it for it in p["repos"][0]["items"] if it["task"] == "t1")["why"] == "contained in hive/t1b", p
assert "(includes hive/t1)" in "\n".join(M.describe(p)), M.describe(p)

# main moved after the plan: the digest no longer matches, nothing changes.
p = M.plan("g3", "k")
sh("git", "-C", str(P), "update-ref", "refs/heads/main", new, c0)
try:
    M.run("g3", "k", p["digest"], "test")
    raise AssertionError("ran on a moved main")
except M.Refused as e:
    assert "plan changed" in str(e), e
assert main_of() == new

# The summary's hive-merge block decides: only t2, though the goal's task has t1.
sh("git", "-C", str(P), "update-ref", "refs/heads/main", c0, new)
p = M.plan("gblock", "k")
assert p["source"] == "the summary's hive-merge block" and actions(p) == [("t2", "fast-forward")], (p["source"], actions(p))
assert p["repos"][0]["items"][0]["note"] == "t2 only", p
assert "fast-forward to hive/t2" in "\n".join(M.describe(p)) and ": t2 only" in "\n".join(M.describe(p))
p = M.plan("gnone", "k")
assert p["nothing"] and not p["repos"] and not p["problems"], p
p = M.plan("gbad", "k")
assert not p["mergeable"] and "not a reviewed commit on the record" in p["problems"][0], p

# Only a completed or accepted goal.
p = M.plan("active", "k")
assert not p["mergeable"] and actions(p) == [("t2", "fast-forward")], p
try:
    M.run("active", "k", p["digest"], "test")
    raise AssertionError("merged an active goal")
except M.Refused as e:
    assert "only a completed or accepted goal" in str(e), e

# A mirror whose commit is already in: not mentioned at all.
p = M.plan("g5", "k")
assert p["nothing"] and M.describe(p) == [], M.describe(p)

# Block syntax.
for bad, why in [("```hive-merge\nproj\n```\n", "expected"), ("```hive-merge\nproj zz12345\n```\n", "expected"),
                 ("```hive-merge\nproj abcdef1\n", "isn't closed"),
                 ("```hive-merge\nnone\n```\n```hive-merge\nnone\n```\n", "more than one")]:
    try:
        M.parse_block(bad)
        raise AssertionError(bad)
    except M.Refused as e:
        assert why in str(e), (bad, e)
assert M.parse_block("no block") is None
assert M.parse_block("```hive-merge\n# a comment\nproj abcdef1 why it\n```\n") == [("proj", "abcdef1", "why it")]
print("merge_test: ok")
