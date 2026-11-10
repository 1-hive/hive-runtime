#!/usr/bin/env python3
"""review-wait.py (R5: a worker waits for its independent review in its own session) against a stand-in
`hive` that serves a task from a file: every exit status, the review text read from the workspace clone, the
in-session fix limit per attempt, an old verdict from before a restart, and the overall wait limit."""
import json, os, shutil, subprocess, sys, tempfile, time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
T = Path(tempfile.mkdtemp())
DIR, BIN = T / "task", T / "bin"
DIR.mkdir(); BIN.mkdir()
shutil.copy(HERE / "skills/hive-worker/scripts/review-wait.py", DIR / "review-wait.py")
STATE = T / "task.json"
(BIN / "hive").write_text(f"#!/bin/sh\n[ \"$1\" = task ] && exec cat {STATE}\nexit 1\n")
(BIN / "hive").chmod(0o755)

# The workspace clone, with the review report the reviewer pushed.
ORIGIN = T / "origin.git"
git = lambda *a, cwd=None: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()
git("init", "-q", "--bare", str(ORIGIN))
SEED = T / "seed"
git("clone", "-q", str(ORIGIN), str(SEED))
(SEED / "reports").mkdir()
(SEED / "reports" / "r-review.md").write_text("# Review\n\n## Verdict: failed\n\n### 1. counter2 is blind (blocking)\n")
git("add", ".", cwd=SEED)
git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "review", cwd=SEED)
git("push", "-q", "origin", "HEAD", cwd=SEED)
SHA = git("rev-parse", "HEAD", cwd=SEED)
git("clone", "-q", str(ORIGIN), str(DIR / "workspace"))

ENV = {**os.environ, "PATH": f"{BIN}:{os.environ['PATH']}", "HIVE_VIA": "claude-code:worker.claude.1",
       "REVIEW_WAIT_SLICE": "1", "REVIEW_WAIT_POLL": "0"}
ev = lambda pos, typ, **data: {"position": pos, "type": typ, "data": data}
PIN = {"commit_oid": f"sha1:{SHA}", "path": "reports/r-review.md", "repository": "workspace"}


def run(task: dict, events: list, env: dict | None = None) -> tuple[int, str]:
    STATE.write_text(json.dumps({"task": {"id": "t1", "owner": "worker.claude.1", **task}, "events": events}))
    r = subprocess.run([sys.executable, str(DIR / "review-wait.py"), "t1"], capture_output=True, text=True,
                       env={**ENV, **(env or {})})
    return r.returncode, r.stdout + r.stderr


RES = {"event_id": "res-0000000001", "author": "worker.claude.1", "position": 12}
BASE = [ev(10, "task.assigned"), ev(12, "task.result_posted")]

rc, out = run({"status": "in_review", "current_result": RES, "latest_review": None}, BASE)
assert rc == 75 and "Still waiting" in out, (rc, out)

rc, out = run({"status": "in_review", "current_result": RES, "assigned_reviewer": "reviewer.codex.1",
               "latest_review": {"verdict": "passed", "reviewer": "reviewer.codex.1", "position": 14}},
              BASE + [ev(14, "review.recorded", verdict="passed")])
assert rc == 0 and "passed" in out, (rc, out)

FAILED = {"verdict": "failed", "reviewer": "reviewer.codex.1", "position": 14, "review_pin": PIN}
rc, out = run({"status": "in_progress", "current_result": RES, "latest_review": FAILED},
              BASE + [ev(14, "review.recorded", verdict="failed")])
assert rc == 10 and "counter2 is blind" in out and "in this session" in out, (rc, out)

# Two fixes in this session already: the third failed verdict ends the attempt.
many = BASE + [ev(14, "review.recorded", verdict="failed"), ev(15, "task.result_posted"),
               ev(17, "review.recorded", verdict="needs_information"), ev(18, "task.result_posted"),
               ev(20, "review.recorded", verdict="failed")]
rc, out = run({"status": "in_progress", "current_result": {**RES, "position": 18},
               "latest_review": {**FAILED, "position": 20}}, many)
assert rc == 11 and "checkpoint" in out, (rc, out)
rc, _ = run({"status": "in_progress", "current_result": {**RES, "position": 18},
             "latest_review": {**FAILED, "position": 20}}, many, {"REVIEW_FIXES_IN_SESSION": "3"})
assert rc == 10, "the limit is configurable"

# A restart resets the count: earlier attempts' failed reviews don't count against this one.
rc, _ = run({"status": "in_progress", "current_result": {**RES, "position": 23},
             "latest_review": {**FAILED, "position": 24}},
            many + [ev(21, "task.restarted"), ev(23, "task.result_posted"), ev(24, "review.recorded", verdict="failed")])
assert rc == 10, rc

# A verdict from before this attempt (restarted, nothing posted yet): post first.
rc, out = run({"status": "in_progress", "current_result": RES, "latest_review": FAILED},
              BASE + [ev(14, "review.recorded", verdict="failed"), ev(16, "task.restarted")])
assert rc == 2 and "Post your result first" in out, (rc, out)

for status, extra in (("cancelled", {}), ("in_progress", {"owner": "worker.claude.2"}), ("blocked", {})):
    rc, out = run({"status": status, "current_result": RES, "latest_review": None, **extra}, BASE)
    assert rc == 2, (status, rc, out)
rc, _ = run({"status": "done", "current_result": RES, "latest_review": None}, BASE)
assert rc == 0
rc, out = run({"status": "in_progress", "current_result": None, "latest_review": None}, BASE[:1])
assert rc == 2 and "no result" in out, (rc, out)

# Waited past REVIEW_WAIT_MAX on this result (the mark is kept in the task folder across calls).
(DIR / f".review-wait-{RES['event_id'][:13]}").write_text(str(time.time() - 7000))
rc, out = run({"status": "in_review", "current_result": RES, "latest_review": None}, BASE)
assert rc == 12 and "No verdict" in out, (rc, out)

print("review_wait test: OK")
