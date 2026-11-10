#!/usr/bin/env python3
"""Wait, inside a worker's own session, for the independent review of its current result (One Hive R5).

    python3 {DIR}/review-wait.py <task-id>       (after `source {DIR}/hive.env`)

The worker runs this right after `task.result_posted`, in the foreground. The dispatcher still picks and
launches the reviewer (a worker can't assign its own: the record allows `review.assigned` only to a
coordinator or the operator), so the review stays independent; what changes is that the verdict comes back
to the session that did the work, with its context, instead of to a fresh restart.

Each call returns within WAIT_SLICE seconds (default 540), under a harness's 10-minute command limit.
Exit status, and what the worker does next:

    0   passed (or the task is closed): end the session.
    10  failed / needs_information: the review is printed; address every finding in this session, post a new
        result, and run this again.
    11  failed, and this attempt has had REVIEW_FIXES_IN_SESSION failed reviews already: post a checkpoint and
        end the session; the supervisor restarts the task with the review attached (and may route it up a tier).
    12  no verdict after REVIEW_WAIT_MAX seconds (default 6000) of waiting on this result: end the session; if
        the review fails later, the supervisor restarts the task with it, as before R5.
    75  still waiting: run it again.
    2   the task isn't waiting for a review of this worker's result (cancelled, reassigned, no result, ...).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

SLICE = int(os.environ.get("REVIEW_WAIT_SLICE", 540))
WAIT_MAX = int(os.environ.get("REVIEW_WAIT_MAX", 6000))
FIXES = int(os.environ.get("REVIEW_FIXES_IN_SESSION", 2))
POLL = int(os.environ.get("REVIEW_WAIT_POLL", 20))
DIR = Path(__file__).resolve().parent


def task(tid: str) -> dict:
    r = subprocess.run(["hive", "task", tid], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return json.loads(r.stdout)


def attempt_start(events: list[dict]) -> int:
    """Position where this attempt began: the latest restart or (re)assignment of the task."""
    return max((e["position"] for e in events
                if e["type"] in ("task.restarted", "task.assigned", "task.reassigned")), default=0)


def failed_in_attempt(events: list[dict]) -> int:
    start = attempt_start(events)
    return sum(1 for e in events if e["type"] == "review.recorded" and e["position"] > start
               and (e.get("data") or {}).get("verdict") != "passed")


def review_text(pin: dict | None) -> str:
    """The review report, from the workspace clone (the reviewer pushes it to workspace main)."""
    if not pin or not pin.get("path"):
        return "(the review has no report path; `hive task` shows its pin)"
    c = pin["commit_oid"].split(":", 1)[-1]
    ws = DIR / "workspace"
    subprocess.run(["git", "-C", str(ws), "fetch", "-q", "origin"], capture_output=True)
    r = subprocess.run(["git", "-C", str(ws), "show", f"{c}:{pin['path']}"], capture_output=True, text=True)
    if r.returncode != 0:
        return f"(couldn't read {pin['path']} at {c[:12]}: {r.stderr.strip()[:200]}; pull the workspace and read it)"
    return f"--- {pin['path']} (workspace {c[:12]}) ---\n{r.stdout}"


def check(tid: str, me: str | None, result: str) -> tuple[int, str] | None:
    """(exit status, message) once there is something to do, else None (keep waiting)."""
    d = task(tid)
    t, events = d["task"], d.get("events") or []
    status, lr = t.get("status"), (t.get("latest_review") or {})
    if status == "done":
        return 0, f"{tid} is closed: its review passed. End your session."
    if status not in ("in_review", "in_progress") or (me and t.get("owner") and t["owner"] != me):
        return 2, f"{tid} is {status}, owner {t.get('owner')}: nothing to wait for here. End your session."
    if (t.get("current_result") or {}).get("event_id") != result:
        return 2, f"{tid}'s current result changed while waiting; run review-wait.py again."
    if status == "in_progress" and (not lr or lr.get("position", 0) < attempt_start(events)):
        # in progress with no verdict on this attempt's result: the worker hasn't posted yet
        return 2, f"{tid} has no result of this attempt under review. Post your result first, then run this."
    if status == "in_review" and lr.get("verdict") == "passed":
        return 0, f"The independent review of your result passed ({lr.get('reviewer')}). Your work is done: end your session; the chief of staff closes the task."
    if status == "in_progress" and lr.get("verdict") in ("failed", "needs_information"):
        n = failed_in_attempt(events)
        head = f"The independent review of your result said `{lr['verdict']}` ({lr.get('reviewer')}).\n\n{review_text(lr.get('review_pin'))}\n"
        if n > FIXES:
            return 11, head + (f"\nThis attempt has had {n} reviews that didn't pass. Don't fix it in this session: commit a "
                               "checkpoint report (which findings remain and how you'd address them), emit `task.reported` "
                               "with `kind: checkpoint`, then end your session. The supervisor restarts the task with this review.")
        return 10, head + ("\nAddress every finding now, in this session (worker contract, \"Result\"): fix, re-run the checks the "
                           "review names, commit, post a new result, then run review-wait.py again. If a finding needs a "
                           "decision outside the order, raise `task.blocked` instead.")
    return None


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    tid = sys.argv[1]
    me = os.environ.get("HIVE_VIA", "").split(":", 1)[-1] or None
    t = task(tid)["task"]
    cur = (t.get("current_result") or {}).get("event_id")
    if not cur:
        print(f"{tid} has no result yet. Post your result first.")
        return 2
    # How long we've waited on this result, across calls (kept in the task folder).
    mark = DIR / f".review-wait-{cur[:13]}"
    if not mark.exists():
        mark.write_text(str(time.time()))
    began = float(mark.read_text() or time.time())
    end = time.time() + SLICE
    while True:
        out = check(tid, me, cur)
        if out:
            print(out[1])
            return out[0]
        waited = time.time() - began
        if waited > WAIT_MAX:
            print(f"No verdict after {int(waited / 60)} min. End your session (don't post anything more): if the review "
                  "fails, the supervisor restarts the task with it.")
            return 12
        if time.time() >= end:
            who = task(tid)["task"].get("assigned_reviewer") or "no reviewer assigned yet"
            print(f"Still waiting ({int(waited / 60)} min so far; reviewer: {who}). Run review-wait.py again.")
            return 75
        time.sleep(POLL)


if __name__ == "__main__":
    sys.exit(main())
