#!/usr/bin/env python3
"""1-hive dispatcher: starts the agents the record calls for, without the chief of staff.

Deterministic, no model (1-hive PLAN, Phase E items 1 and 2). Every tick it reads the record
as actor `dispatcher` (class coordinator, its own key) and:

- **Workers.** For each task `assigned` to a worker whose task folder has no attempt yet, it
  writes the kickoff from the record and launches the owner (launch-task.sh). A launch the
  launcher refuses (e.g. a bad `Route facts:` line in the order) is retried every RETRY seconds
  and kept in .dispatcher/refused.json; the supervisor escalates it to the chief of staff. For each
  `blocked` task answered since its block (`task.answered`) whose worker isn't running, it
  relaunches the owner to read the answer, unblock and continue. The chief of staff only
  creates and assigns tasks and answers questions; restarts stay with the supervisor.
- **Reviews.** For each task `in_review` whose current result has no reviewer assigned yet:

1. writes the reviewer's kickoff from the record (goal, order, result, its code refs);
2. emits `review.assigned` to the first reviewer that isn't the result's author
   (REVIEWERS, in order) and launches it through launch-task.sh;
3. if the router has no route for that reviewer now (launcher exit 3: capacity, or the
   review-family rule), tries the next one; if none can run, retries after RETRY seconds.

With HIVE_REVIEW_ROUTE_TABLE (review_routes.py), a result for long-lived reviewers goes where the
router says instead: the review's tier from the order's `Route facts:` and `Review tier:` lines, the
first reviewer in that tier that can run (the Floor's view), and RT4 against the result's author.
A result waiting on a reviewer the Floor has shown unable to run for HIVE_REVIEW_MOVE_SECONDS is
moved to the reviewer the router picks then. If the router can't be asked or gives no route, the
REVIEWERS order applies, as without a table.

An assigned reviewer is relaunched (not re-assigned) when its launch never happened, or when
its process ended without a verdict on a usage or quota limit (class `capacity`: the launcher
then holds that pool, so the router picks another or waits; the supervisor tells the operator
if it keeps happening).

Skipped: tasks with an open escalation (the chief of staff is handling them), except a result
posted after the escalation, which is reviewed anyway (`held()`). The verdict
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

import hive_paths as hp
import review_routes as rr
from supervisor import alive, failure_class, latest_attempt, long_lived

ROOT = hp.WORK_DIR
HOME = ROOT / ".dispatcher"
LAUNCH = Path(__file__).resolve().parent / "launch-task.sh"
ENV = hp.tool_env(hp.CONFIG_DIR / "agents" / "dispatcher.key", "dispatcher")
REVIEWERS = tuple(r.strip() for r in os.environ.get("HIVE_REVIEWERS", "").split(",") if r.strip()) \
    or ("reviewer.codex.1", "reviewer.claude.1")   # in order of preference (HIVE_REVIEWERS overrides)
RETRY = 600
WAIT_RETRY = 300   # a routed review no reviewer can take now: ask the router again after this
ROUTE = ROOT / ".route"
REFUSED = HOME / "refused.json"   # task -> why its worker launch was refused (the supervisor reads it)

WORKER_KICKOFF = """You are **{owner}** in the 1-hive record, assigned task **{task}** ("{title}"; goal `{goal}`, project `{project}`). You run in your own container.

Read first: `{{DIR}}/WORKER-CONTRACT.md`, then, in `{{DIR}}/workspace` (pull main first), your order `{order_path}` and its goal `{goal_path}`.

Before any `hive` or `hive-pin` command: `source {{DIR}}/hive.env` (your key only). {repos_line} Follow the contract's "Running mode": never end your session to wait; run long steps in the foreground, in pieces up to 10 minutes.
"""

ANSWERED_KICKOFF = """You are **{owner}**, relaunched on task **{task}** ("{title}"; goal `{goal}`, project `{project}`), in a new container of your own. You were blocked, and your question has been answered: the answer is `{answer_path}` in the workspace (pull main), also on the record (`hive task {task}`, the latest `task.answered`).

Your task folder `{{DIR}}` is as you left it, but this is a fresh container: anything you ran in the last one is gone.

1. `source {{DIR}}/hive.env`, read the answer, then emit `task.unblocked`.
2. Continue the order: `{{DIR}}/workspace/{order_path}`. Read `{{DIR}}/WORKER-CONTRACT.md` again, especially "Running mode".
"""
CODE_HOME = os.environ.get("HIVE_DEFAULT_REPO") or "mtg-player"   # the repository every task folder holds; others come as EXTRA_REPOS

KICKOFF = """You are **{reviewer}**, an independent reviewer in the 1-hive record, in your own container. You review task **{task}** (goal `{goal}`, project `{project}`): its current result, event `{result_event}`.

Read first:
- `{{DIR}}/WORKER-CONTRACT.md`: "Reviewers", "Identity", "Stop-lines" and "Running mode";
- in `{{DIR}}/workspace` (pull main): the goal `{goal_path}`, especially "Doesn't count, even if every check passes, when", and the order `{order_path}`.

{result_line} {code_line}

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


def relaunch_of(t: dict, events: list[dict]) -> tuple[str, str | None] | None:
    """(reviewer, class) for the reviewer assigned to the current result that must be launched
    again: its launch never happened (no attempt pid since the assignment; class None), or its
    process ended without a verdict on a usage limit (class `capacity`)."""
    cur = (t.get("current_result") or {}).get("event_id")
    asg = [e for e in events if e["type"] == "review.assigned" and (e.get("data") or {}).get("result_event") == cur]
    if not asg:
        return None
    rv = asg[-1]["data"]["reviewer"]
    if long_lived(rv):      # it runs elsewhere (a reviewer runner, an agent's sidecar): assigning is all we do
        return None
    at = dt.datetime.fromisoformat(asg[-1]["recorded_at"].replace("Z", "+00:00")).timestamp()
    pids = ROUTE.joinpath(f"{t['id']}-review").glob("review.*.pid")
    if not any(p.stat().st_mtime >= at for p in pids):
        return rv, None
    att = latest_attempt(t["id"], "review")
    if att and not alive(att[1]) and failure_class(t["id"], "review", att[0]) == "capacity":
        return rv, "capacity"
    return None


def held(t: dict) -> bool:
    """An open escalation holds the task for the chief of staff, unless the task's current result was
    posted after it: the result answers it (a "stuck" worker that reports back and posts), and the
    review goes ahead while the chief of staff still sees the escalation. On the proto-hive (2026-11-08)
    a stuck escalation from 02:12 held F3's 03:28 result for 13 hours, with nothing in the log, until
    the chief of staff resolved it and assigned the review by hand."""
    esc = (t.get("ext") or {}).get("escalation")
    if not esc:
        return False
    cur = t.get("current_result") or {}
    return not (t["status"] == "in_review" and cur.get("position", 0) > esc.get("position", 0))


def needs_review(t: dict, events: list[dict]) -> bool:
    cur = (t.get("current_result") or {}).get("event_id")
    if t["status"] != "in_review" or not cur or held(t):
        return False
    if any(e["type"] == "review.recorded" and (e.get("data") or {}).get("result_event") == cur for e in events):
        return False
    return not any(e["type"] == "review.assigned" and (e.get("data") or {}).get("result_event") == cur
                   for e in events) or relaunch_of(t, events) is not None


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
    review_path = f"projects/{t.get('project') or CODE_HOME}/reports/{day}-{task}-review{'' if n == 0 else f'-{n + 1}'}.md"
    text = KICKOFF.format(
        reviewer=reviewer, task=task, goal=t.get("goal"), project=t.get("project"),
        result_event=res["event_id"], goal_path=(goal.get("goal_pin") or {}).get("path", "(see the order)"),
        order_path=t["order"].get("path", "(see the goal)"), result_line=result_line(res["pin"]),
        code_line=code_line, review_path=review_path)
    HOME.mkdir(parents=True, exist_ok=True)
    p = HOME / f"{task}-{res['event_id'][:13]}-{reviewer}.md"
    p.write_text(text)
    return p


def result_line(pin: dict) -> str:
    """Where the reviewer finds the result: a file at a commit, or a whole commit (R0 format 2
    allows a pin without a path), whose files the reviewer lists."""
    c = pin["commit_oid"].split(":", 1)[-1]
    if pin.get("path"):
        return (f"The result is `{pin['path']}`, at workspace commit `{c}` (`git -C {{DIR}}/workspace fetch origin && "
                f"git -C {{DIR}}/workspace show {c}:{pin['path']}`).")
    return (f"The result is the whole workspace commit `{c}`, not one file: `git -C {{DIR}}/workspace fetch origin && "
            f"git -C {{DIR}}/workspace show --stat {c}` lists what it adds; the report is among them.")


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
    note_refusal(task, r)
    if r.returncode != 0:
        retry_at[key] = time.time() + RETRY
    else:
        done.write_text(json.dumps([*launched, key][-1000:]))


def note_refusal(task: str, r: subprocess.CompletedProcess) -> None:
    """Keep why the task's worker couldn't be launched (REFUSED; cleared by a launch that works). The
    dispatcher may not escalate (class coordinator); the supervisor escalates it to the chief of staff."""
    try:
        refused = json.loads(REFUSED.read_text())
    except (OSError, ValueError):
        refused = {}
    if r.returncode == 0:
        if refused.pop(task, None) is None:
            return
    else:
        refused[task] = {"at": time.time(), "rc": r.returncode, "reason": r.stderr.strip()[-400:]}
    REFUSED.write_text(json.dumps(refused))


def fields(t: dict, state: dict) -> dict:
    goal = state.get("goals", {}).get(t.get("goal") or "", {})
    return {"owner": t["owner"], "task": t["id"], "title": t.get("title", ""), "goal": t.get("goal"),
            "project": t.get("project"), "order_path": t["order"].get("path", "(see the goal)"),
            "goal_path": (goal.get("goal_pin") or {}).get("path", "(see the order)")}


def assign(t: dict, reviewer: str, dry: bool, why: str) -> None:
    """Assign-only: a reviewer runner or the agent's own sidecar takes it from the record, with its own kickoff."""
    task, res = t["id"], t["current_result"]
    if dry:
        log(f"DRY assign {reviewer} to {task} ({res['event_id']}), no launch; {why}")
        return
    ev = json.loads(hive("emit", "review.assigned", "--task", task, "--cause-event", res["event_id"], "--data",
                         json.dumps({"reviewer": reviewer, "result_event": res["event_id"]})))
    log(f"review.assigned {task} -> {reviewer} at position {ev['position']} (runs elsewhere; {why})")


def routed(t: dict, dry: bool, retry_at: dict) -> bool:
    """Assign the current result to the long-lived reviewer the router picks. False: use REVIEWERS."""
    task, res = t["id"], t["current_result"]
    try:
        d = rr.decide(t)
    except Exception as e:
        log(f"{task}: review routing failed, using HIVE_REVIEWERS: {e}")
        return False
    actor = d.get("actor")
    if d["decision"] == "route" and actor and long_lived(actor) and actor != res.get("author"):
        assign(t, actor, dry, f"routed: {d['route_id']}, tier {d['tier']}")
        return True
    if d["decision"] == "wait":
        retry_at[res["event_id"]] = time.time() + WAIT_RETRY
        log(f"{task}: no reviewer can take it now ({rr.why(d)}); asking again in {WAIT_RETRY // 60} min")
        return True
    note = "" if not actor or long_lived(actor) else f", {actor} is not long-lived"
    log(f"{task}: the router gave {rr.why(d)}{note}; using HIVE_REVIEWERS")
    return False


def maybe_move(t: dict, dry: bool, retry_at: dict, events: list[dict]) -> None:
    """Move a waiting result off a routed reviewer the Floor has shown unable to run for MOVE_AFTER."""
    task, res = t["id"], t.get("current_result") or {}
    cur = res.get("event_id")
    if t["status"] != "in_review" or not cur or time.time() < retry_at.get(f"move:{cur}", 0):
        return
    if any(e["type"] == "review.recorded" and (e.get("data") or {}).get("result_event") == cur for e in events):
        return
    asg = [e for e in events if e["type"] == "review.assigned" and (e.get("data") or {}).get("result_event") == cur]
    rv = asg[-1]["data"]["reviewer"] if asg else None
    if not rv or rv not in rr.actors() or not rr.should_move(rv):
        return
    try:
        d = rr.decide(t, "reassign", moving_off=rv)
    except Exception as e:
        log(f"{task}: review routing failed, {rv} keeps it: {e}")
        retry_at[f"move:{cur}"] = time.time() + WAIT_RETRY
        return
    actor = d.get("actor")
    if d["decision"] == "route" and actor and actor != rv and long_lived(actor) and actor != res.get("author"):
        assign(t, actor, dry, f"moved off {rv}, which can't run: {rr.floor_note.get(rv, '?')}; "
                              f"routed: {d['route_id']}, tier {d['tier']}")
        return
    retry_at[f"move:{cur}"] = time.time() + WAIT_RETRY
    log(f"{task}: {rv} can't run and no other reviewer can take it ({rr.why(d)}); asking again in {WAIT_RETRY // 60} min")


def dispatch(t: dict, state: dict, dry: bool, retry_at: dict, events: list[dict]) -> None:
    task, res = t["id"], t["current_result"]
    if time.time() < retry_at.get(res["event_id"], 0):
        return
    author = res.get("author")
    repos = (t.get("ext") or {}).get("repos") or []
    extra = " ".join(r for r in repos if r != CODE_HOME)
    again = relaunch_of(t, events)   # already assigned, not running: relaunch, don't re-assign
    if not again and rr.enabled() and routed(t, dry, retry_at):
        return
    for reviewer in ([again[0]] if again else [r for r in REVIEWERS if r != author]):
        if long_lived(reviewer) and not again:
            assign(t, reviewer, dry, "first in HIVE_REVIEWERS")
            return
        path = kickoff(t, reviewer, state)
        if dry:
            log(f"DRY {'relaunch' if again else 'assign'} {reviewer} to {task} ({res['event_id']}), kickoff {path}")
            return
        if again:
            log(f"relaunch {task} -> {reviewer} ({'hit a usage limit' if again[1] else 'assigned, never started'})")
        else:
            ev = json.loads(hive("emit", "review.assigned", "--task", task, "--cause-event", res["event_id"], "--data",
                                 json.dumps({"reviewer": reviewer, "result_event": res["event_id"]})))
            log(f"review.assigned {task} -> {reviewer} at position {ev['position']}")
        r = subprocess.run([str(LAUNCH), "reviewer", task, reviewer, str(path)], capture_output=True,
                           text=True, env={**os.environ, **({"EXTRA_REPOS": extra} if extra else {}),
                                           **({"ROUTE_LAST_CLASS": again[1]} if again and again[1] else {})})
        log(f"launch {task} {reviewer}: rc={r.returncode} {r.stdout.strip()[-200:]} {r.stderr.strip()[-300:]}")
        if r.returncode == 0:
            return
        if r.returncode != 3:   # not a routing outcome: don't try the next reviewer blindly
            break
    retry_at[res["event_id"]] = time.time() + RETRY
    log(f"no reviewer could start for {task}; retry in {RETRY // 60} min")


def tick(dry: bool, retry_at: dict) -> None:
    state = json.loads(hive("state"))
    if rr.enabled():
        rr.read_floor()
    for t in state.get("tasks", {}).values():
        try:   # one task's failure is logged, never stops the others
            handle(t, state, dry, retry_at)
        except Exception as e:
            log(f"{t['id']}: {e!r}")


def handle(t: dict, state: dict, dry: bool, retry_at: dict) -> None:
    """Launch what one task needs: its worker, a worker to read an answer, or its reviewer."""
    if held(t):
        return
    if long_lived(t.get("owner")) and t["status"] != "in_review":
        return      # its sidecar delivers assignments and answers; nothing to launch
    if t["status"] == "assigned" and t.get("owner", "").startswith("worker.") \
            and not any(ROUTE.joinpath(t["id"]).glob("worker.*.pid")):
        launch_worker(t, state, WORKER_KICKOFF.format(**fields(t, state), repos_line=repos_line(t)),
                      dry, retry_at, "new")
    elif t["status"] == "blocked" and not worker_alive(t["id"]):
        events = json.loads(hive("task", t["id"]))["events"]
        blocked = max((e["position"] for e in events if e["type"] == "task.blocked"), default=0)
        answers = [e for e in events if e["type"] == "task.answered" and e["position"] > blocked]
        if answers:
            ans = next((r["pin"].get("path", "?") for r in answers[-1].get("refs", []) if r["rel"] == "answer"), "?")
            launch_worker(t, state, ANSWERED_KICKOFF.format(**fields(t, state), answer_path=ans),
                          dry, retry_at, f"answered-{answers[-1]['position']}")
    elif t["status"] == "in_review":
        events = json.loads(hive("task", t["id"]))["events"]
        if needs_review(t, events):
            dispatch(t, state, dry, retry_at, events)
        elif rr.enabled():
            maybe_move(t, dry, retry_at, events)


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
