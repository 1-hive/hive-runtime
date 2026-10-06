#!/usr/bin/env python3
"""1-hive dispatcher: starts the agents the record calls for, without the chief of staff.

Deterministic, no model (1-hive PLAN, Phase E items 1 and 2). Every tick it reads the record
as actor `dispatcher` (class coordinator, its own key) and:

- **Workers.** For each task `assigned` to a worker whose task folder has no attempt yet, it
  writes the kickoff from the record and launches the owner (tools/launch-task.sh). For each
  `blocked` task answered since its block (`task.answered`) whose worker isn't running, it
  relaunches the owner to read the answer, unblock and continue. The chief of staff only
  creates and assigns tasks and answers questions; restarts stay with the supervisor.
- **Reviews.** For each task `in_review` whose current result has no reviewer assigned yet:

1. writes the reviewer's kickoff from the record (goal, order, result, its code refs);
2. emits `review.assigned` to the first reviewer that isn't the result's author
   (REVIEWERS, in order) and launches it through tools/launch-task.sh;
3. if the router has no route for that reviewer now (launcher exit 3: capacity, or the
   review-family rule), tries the next one; if none can run, retries after RETRY seconds.

Skipped: tasks with an open escalation (the chief of staff is handling them). The verdict
goes straight to the record; a failed review restarts the worker (supervisor), a passed
one waits for the chief of staff to close the task.

    dispatcher.py [--interval 60] [--once] [--dry-run]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path.home() / "work" / "1hive"
HOME = ROOT / ".dispatcher"
LAUNCH = Path(__file__).resolve().parent / "launch-task.sh"
ENV = {**os.environ, "HIVE_URL": "http://127.0.0.1:8470", "HIVE_ID": "1-hive",
       "HIVE_KEY_FILE": str(Path.home() / ".config/hive/agents/dispatcher.key"),
       "HIVE_VIA": "dispatcher", "PATH": f"{Path.home()}/.local/bin:{os.environ.get('PATH', '')}"}
REVIEWERS = ("reviewer.codex.1", "reviewer.claude.1")   # in order of preference
RETRY = 600
ROUTE = ROOT / ".route"

WORKER_KICKOFF = """You are **{owner}** in the 1-hive record, assigned task **{task}** ("{title}"; goal `{goal}`, project `{project}`). You run in your own container.

Read first: `{{DIR}}/WORKER-CONTRACT.md`, then, in `{{DIR}}/workspace` (pull main first), your order `{order_path}` and its goal `{goal_path}`.

Before any `hive` or `hive-pin` command: `source {{DIR}}/hive.env` (your key only). {repos_line} Follow the contract's "Running mode": never end your session to wait; run long steps in the foreground, in pieces up to 10 minutes.
"""

ANSWERED_KICKOFF = """You are **{owner}**, relaunched on task **{task}** ("{title}"; goal `{goal}`, project `{project}`), in a new container of your own. You were blocked, and your question has been answered: the answer is `{answer_path}` in the workspace (pull main), also on the record (`hive task {task}`, the latest `task.answered`).

Your task folder `{{DIR}}` is as you left it, but this is a fresh container: anything you ran in the last one is gone.

1. `source {{DIR}}/hive.env`, read the answer, then emit `task.unblocked`.
2. Continue the order: `{{DIR}}/workspace/{order_path}`. Read `{{DIR}}/WORKER-CONTRACT.md` again, especially "Running mode".
"""
CODE_HOME = "mtg-player"   # the repository every task folder holds; others come as EXTRA_REPOS

KICKOFF = """You are **{reviewer}**, an independent reviewer in the 1-hive record, in your own container. You review task **{task}** (goal `{goal}`, project `{project}`): its current result, event `{result_event}`.

Read first:
- `{{DIR}}/WORKER-CONTRACT.md`: "Reviewers", "Identity", "Stop-lines" and "Running mode";
- in `{{DIR}}/workspace` (pull main): the goal `{goal_path}`, especially "Doesn't count, even if every check passes, when", and the order `{order_path}`.

The result is `{result_path}`, at workspace commit `{result_commit}` (`git -C {{DIR}}/workspace fetch origin && git -C {{DIR}}/workspace show {result_commit}:{result_path}`). {code_line}

Before any `hive` or `hive-pin` command: `source {{DIR}}/hive.env` (your key only).

Judge the result against the order and the goal. Re-run what matters, in your container; don't trust the report's own claims. Check that nothing was skipped or weakened, and that nothing outside the order's scope changed. Stop every container you start.

Write `{review_path}` on workspace **main**: the verdict, findings with evidence, and what would change the verdict. Commit, pull --rebase, push, then mint its pin (`hive-pin mint workspace {review_path} --output review.pin`), then:
`hive emit review.recorded --task {task} --data '{{"verdict":"<passed|failed|needs_information>","result_event":"{result_event}"}}' --ref review=review.pin`

Don't fix the work, and don't push anywhere except workspace main.
"""


def log(msg: str) -> None:
    print(f"{dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%M:%SZ} {msg}", flush=True)


def hive(*args: str) -> str:
    r = subprocess.run(["hive", *args], capture_output=True, text=True, env=ENV)
    if r.returncode != 0:
        raise RuntimeError(f"hive {' '.join(args[:2])}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def needs_review(t: dict, events: list[dict]) -> bool:
    cur = (t.get("current_result") or {}).get("event_id")
    if t["status"] != "in_review" or not cur or (t.get("ext") or {}).get("escalation"):
        return False
    return not any(e["type"] in ("review.assigned", "review.recorded")
                   and (e.get("data") or {}).get("result_event") == cur for e in events)


def kickoff(t: dict, reviewer: str, state: dict) -> Path:
    task, res = t["id"], t["current_result"]
    goal = state.get("goals", {}).get(t.get("goal") or "", {})
    repos = (t.get("ext") or {}).get("repos") or []
    code_line = (f"It changes {', '.join(f'`{r}`' for r in repos)}: run `./result-refs.sh {task}` for each "
                 "repository's base and code, review `git diff <base>..<code>` in `{DIR}/<repo>`, and run the "
                 "checks at `<code>`." if repos else
                 "The order names no code repositories: review the commits and evidence the report names.")
    n = len(list((ROOT / f"{task}-review").glob("*.log")) + list((ROOT / f"{task}-review").glob("*.jsonl"))) \
        if (ROOT / f"{task}-review").is_dir() else 0
    day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    review_path = f"projects/{t.get('project') or 'mtg-player'}/reports/{day}-{task}-review{'' if n == 0 else f'-{n + 1}'}.md"
    text = KICKOFF.format(
        reviewer=reviewer, task=task, goal=t.get("goal"), project=t.get("project"),
        result_event=res["event_id"], goal_path=(goal.get("goal_pin") or {}).get("path", "(see the order)"),
        order_path=t["order"]["path"], result_path=res["pin"]["path"],
        result_commit=res["pin"]["commit_oid"].split(":", 1)[-1], code_line=code_line, review_path=review_path)
    HOME.mkdir(parents=True, exist_ok=True)
    p = HOME / f"{task}-{res['event_id'][:13]}-{reviewer}.md"
    p.write_text(text)
    return p


def repos_line(t: dict) -> str:
    repos = (t.get("ext") or {}).get("repos") or []
    return (f"The order names {', '.join(f'`{r}`' for r in repos)} in `repos`: post `code` and `base` refs for "
            "each (contract, \"Result\"), even if you change nothing (base and code are then the same commit)."
            if repos else "The order names no code repositories: add no code refs to your result.")


def worker_alive(task: str) -> bool:
    """Whether the task's latest worker attempt is still running (its launcher pid)."""
    pids = sorted(ROUTE.joinpath(task).glob("worker.*.pid"), key=lambda p: int(p.name.split(".")[1])) \
        if ROUTE.joinpath(task).is_dir() else []
    if not pids:
        return False
    try:
        os.kill(int(pids[-1].read_text().strip()), 0)
    except PermissionError:   # sudo, for an agent user's container, runs as root
        return True
    except (OSError, ValueError):
        return False
    return True


def launch_worker(t: dict, state: dict, text: str, dry: bool, retry_at: dict, why: str) -> None:
    task = t["id"]
    key = f"worker:{task}:{why}"
    done = HOME / "launched.json"   # one launch per reason: a relaunch that dies is the supervisor's or cos's
    try:
        launched = json.loads(done.read_text())
    except (OSError, ValueError):
        launched = []
    if key in launched or time.time() < retry_at.get(key, 0):
        return
    HOME.mkdir(parents=True, exist_ok=True)
    path = HOME / f"{task}-{why}-{int(time.time())}.md"
    path.write_text(text)
    if dry:
        log(f"DRY launch worker {t['owner']} on {task} ({why}), kickoff {path}")
        return
    extra = " ".join(r for r in ((t.get("ext") or {}).get("repos") or []) if r != CODE_HOME)
    r = subprocess.run([str(LAUNCH), "worker", task, t["owner"], str(path)], capture_output=True, text=True,
                       env={**os.environ, **({"EXTRA_REPOS": extra} if extra else {})})
    log(f"launch worker {task} ({why}): rc={r.returncode} {r.stdout.strip()[-200:]} {r.stderr.strip()[-300:]}")
    if r.returncode != 0:
        retry_at[key] = time.time() + RETRY
    else:
        done.write_text(json.dumps([*launched, key][-1000:]))


def fields(t: dict, state: dict) -> dict:
    goal = state.get("goals", {}).get(t.get("goal") or "", {})
    return {"owner": t["owner"], "task": t["id"], "title": t.get("title", ""), "goal": t.get("goal"),
            "project": t.get("project"), "order_path": t["order"]["path"],
            "goal_path": (goal.get("goal_pin") or {}).get("path", "(see the order)")}


def dispatch(t: dict, state: dict, dry: bool, retry_at: dict) -> None:
    task, res = t["id"], t["current_result"]
    if time.time() < retry_at.get(res["event_id"], 0):
        return
    author = res.get("author")
    repos = (t.get("ext") or {}).get("repos") or []
    extra = " ".join(r for r in repos if r != CODE_HOME)
    for reviewer in (r for r in REVIEWERS if r != author):
        path = kickoff(t, reviewer, state)
        if dry:
            log(f"DRY assign {reviewer} to {task} ({res['event_id']}), kickoff {path}")
            return
        ev = json.loads(hive("emit", "review.assigned", "--task", task, "--data",
                             json.dumps({"reviewer": reviewer, "result_event": res["event_id"]})))
        log(f"review.assigned {task} -> {reviewer} at position {ev['position']}")
        r = subprocess.run([str(LAUNCH), "reviewer", task, reviewer, str(path)], capture_output=True,
                           text=True, env={**os.environ, **({"EXTRA_REPOS": extra} if extra else {})})
        log(f"launch {task} {reviewer}: rc={r.returncode} {r.stdout.strip()[-200:]} {r.stderr.strip()[-300:]}")
        if r.returncode == 0:
            return
        if r.returncode != 3:   # not a routing outcome: don't try the next reviewer blindly
            break
    retry_at[res["event_id"]] = time.time() + RETRY
    log(f"no reviewer could start for {task}; retry in {RETRY // 60} min")


def tick(dry: bool, retry_at: dict) -> None:
    state = json.loads(hive("state"))
    for t in state.get("tasks", {}).values():
        if (t.get("ext") or {}).get("escalation"):
            continue
        if t["status"] == "assigned" and t.get("owner", "").startswith("worker.") \
                and not any(ROUTE.joinpath(t["id"]).glob("worker.*.pid")):
            launch_worker(t, state, WORKER_KICKOFF.format(**fields(t, state), repos_line=repos_line(t)),
                          dry, retry_at, "new")
        elif t["status"] == "blocked" and not worker_alive(t["id"]):
            events = json.loads(hive("task", t["id"]))["events"]
            blocked = max((e["position"] for e in events if e["type"] == "task.blocked"), default=0)
            answers = [e for e in events if e["type"] == "task.answered" and e["position"] > blocked]
            if answers:
                ans = next((r["pin"]["path"] for r in answers[-1].get("refs", []) if r["rel"] == "answer"), "?")
                launch_worker(t, state, ANSWERED_KICKOFF.format(**fields(t, state), answer_path=ans),
                              dry, retry_at, f"answered-{answers[-1]['position']}")
        elif t["status"] == "in_review":
            events = json.loads(hive("task", t["id"]))["events"]
            if needs_review(t, events):
                dispatch(t, state, dry, retry_at)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    log(f"dispatcher up (interval {a.interval}s{', dry run' if a.dry_run else ''})")
    retry_at: dict = {}
    while True:
        try:
            tick(a.dry_run, retry_at)
        except Exception as e:   # one bad tick must not stop dispatching
            log(f"tick failed: {e}")
        if a.once:
            return
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main())
