#!/usr/bin/env python3
"""1-hive supervisor: keeps tasks moving, deterministically (1-hive PLAN D14).

Every tick it reads the record (as actor `supervisor`, with its own key) and,
for each task a worker holds, applies the ladder:

1. The worker's process ended without a result      -> restart it (task.restarted).
2. The worker is alive but silent past its check-in -> task.nudged (recorded; a
   print-mode worker can't receive messages). After 2 nudges with no activity,
   stop it and restart (class `stalled`).
3. A task already restarted 3 times                   -> task.escalated to the chief
   of staff (`repeated_failure`), and the supervisor leaves it alone.
4. A reviewer's process ended without a verdict       -> task.escalated to the chief
   of staff (`other`).

A restart's context is generated from the record (order, last reports, reason),
committed to the workspace and pinned on task.restarted, then the worker is
relaunched through tools/launch-task.sh with ROUTE_LAST_CLASS and the task's
last supplied ROUTE_FACTS. Tasks with an open escalation are skipped.

    supervisor.py [--interval 60] [--once] [--dry-run]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path.home() / "work" / "1hive"
ROUTE = ROOT / ".route"
HOME = ROOT / ".supervisor"
WS = HOME / "workspace"
LAUNCH = Path(__file__).resolve().parent / "launch-task.sh"
REGISTRY_SRC = Path(__file__).resolve().parents[1] / "deploy" / "registry.json"
ENV = {**os.environ, "HIVE_URL": "http://127.0.0.1:8470", "HIVE_ID": "1-hive",
       "HIVE_KEY_FILE": str(Path.home() / ".config/hive/agents/supervisor.key"),
       "HIVE_VIA": "supervisor", "HIVEPIN_REPOSITORY_REGISTRY_PATH": str(HOME / "registry.json"),
       "PATH": f"{Path.home()}/.local/bin:{os.environ.get('PATH', '')}"}
GRACE = 600          # seconds past the check-in interval before acting
MAX_ATTEMPTS = 4     # attempt 1 + 3 restarts, then escalate
MAX_NUDGES = 2


def log(msg: str) -> None:
    print(f"{dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%M:%SZ} {msg}", flush=True)


def hive(*args: str, check: bool = True) -> str:
    r = subprocess.run(["hive", *args], capture_output=True, text=True, env=ENV)
    if check and r.returncode != 0:
        raise RuntimeError(f"hive {' '.join(args[:2])}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(WS), *args], check=True, capture_output=True, text=True).stdout


def setup() -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    if not (WS / ".git").exists():
        subprocess.run(["git", "clone", "-q", "/home/omegahive/repos/hive-workspace.git", str(WS)], check=True)
        git("config", "user.name", "supervisor (1-hive)")
        git("config", "user.email", "supervisor@1-hive.invalid")
    reg = json.loads(REGISTRY_SRC.read_text())
    reg["repositories"]["workspace"]["local_path"] = str(WS)
    (HOME / "registry.json").write_text(json.dumps(reg, indent=2))


def ts(s: str | None) -> float | None:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else None


def latest_attempt(task: str, prefix: str) -> tuple[int, int | None] | None:
    """(n, pid) of the task's latest attempt of this kind, from the launcher's pid files."""
    d = ROUTE / task
    best = None
    for p in d.glob(f"{prefix}.*.pid") if d.is_dir() else []:
        try:
            n = int(p.name.split(".")[1])
            pid = int(p.read_text().strip())
        except ValueError:
            continue
        if best is None or n > best[0]:
            best = (n, pid)
    return best


def alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return not Path(f"/proc/{pid}/status").read_text().count("State:\tZ")


def stop(pid: int) -> None:
    subprocess.run(["pkill", "-TERM", "-P", str(pid)], check=False)
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


CAPACITY_SIGNS = ("out of credits", "session limit", "usage limit", "rate limit", "hit your limit",
                  "quota", "insufficient_quota", "overloaded")


def failure_class(task: str, prefix: str, n: int) -> str:
    """Why the attempt's process ended, from the tail of its output: `capacity` for a
    usage or quota limit (so the router avoids that pool), else `interrupted`."""
    try:
        out = json.loads((ROUTE / task / f"{prefix}.{n}.attempt.json").read_text()).get("output")
        tail = Path(out).read_bytes()[-6000:].decode("utf-8", "replace").lower()
        err = Path(str(Path(out).with_suffix("")) + ".err")
        if err.exists():
            tail += err.read_bytes()[-2000:].decode("utf-8", "replace").lower()
    except (OSError, ValueError, TypeError):
        return "interrupted"
    return "capacity" if any(sign in tail for sign in CAPACITY_SIGNS) else "interrupted"


def supplied_facts(task: str) -> str | None:
    """The facts the chief of staff supplied at the task's last routed launch."""
    log_path = ROOT / "route-log.jsonl"
    facts = None
    if log_path.exists():
        for line in log_path.read_text().splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            dec = (e.get("data") or {}).get("decision") or {}
            # Only the task's own work attempts: a review's facts include kind=review.
            if e.get("type") == "route.decided" and dec.get("task") == task and ".review." not in str(dec.get("attempt", "")):
                f = {k: v["value"] for k, v in (dec.get("facts") or {}).items()
                     if isinstance(v, dict) and v.get("source") == "supplied" and k != "kind"}
                facts = f or facts
    return json.dumps(facts) if facts else None


def emit(etype: str, task: str, data: dict, refs: list[str] = (), dry: bool = False) -> None:
    args = ["emit", etype, "--task", task, "--data", json.dumps(data)]
    for r in refs:
        args += ["--ref", r]
    if dry:
        log(f"DRY {etype} {task} {data}")
        return
    ev = json.loads(hive(*args))
    log(f"{etype} {task} at position {ev['position']}")


def restart(t: dict, events: list[dict], reason: str, cls: str, dry: bool) -> None:
    task, attempt = t["id"], (t.get("ext") or {}).get("attempt", 1)
    reports = [r["pin"]["path"] for e in events if e["type"] == "task.reported" for r in e.get("refs", [])]
    reviews = [e for e in events if e["type"] == "review.recorded"]
    last_review = reviews[-1] if reviews and (reviews[-1].get("data") or {}).get("verdict") != "passed" else None
    review_note = ""
    if last_review:
        rp = next((r["pin"]["path"] for r in last_review.get("refs", []) if r["rel"] == "review"), None)
        review_note = (f"\n**The independent review of your last result said `{last_review['data']['verdict']}`.** "
                       f"Read it first and address every finding: `{{DIR}}/workspace/{rp}` (pull the workspace). "
                       "Then post a new result.\n")
    project = t.get("project") or "mtg-player"
    rel = f"projects/{project}/runs/{task}/restart-{attempt + 1}.md"
    body = f"""# Restart context: {task}, attempt {attempt + 1}

Written by the supervisor from the record at {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC.

You are **{t['owner']}**, restarted by the supervisor on task **{task}** ("{t['title']}", goal `{t.get('goal')}`).
**Why:** {reason}.
{review_note}
Your earlier work is intact: your branch and task folder `{{DIR}}` (clones, hive.env, contract) are as you left them.

Read, in order:
1. `{{DIR}}/WORKER-CONTRACT.md`, especially "Running mode" (never wait on background jobs).
2. Your order: `{{DIR}}/workspace/{t['order']['path']}`.
3. Your latest reports, which say where you were:
{chr(10).join(f"   - `{{DIR}}/workspace/{p}`" for p in dict.fromkeys(reports[-3:])) or "   - (none posted)"}

Then: `source {{DIR}}/hive.env`, pull the workspace, post a checkpoint that says you were restarted, and finish the order. Stop anything left running from the previous attempt (containers, games) before starting new ones.
"""
    if dry:
        log(f"DRY restart {task} ({cls}): {reason}")
        return
    git("pull", "-q", "--rebase")
    path = WS / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    git("add", rel)
    git("commit", "-q", "-m", f"{task}: restart context, attempt {attempt + 1}")
    git("push", "-q", "origin", "HEAD")
    pin = HOME / f"{task}-restart-{attempt + 1}.pin"
    subprocess.run(["hive-pin", "mint", "workspace", rel, "--output", str(pin)], check=True,
                   capture_output=True, env=ENV)
    emit("task.restarted", task, {"reason": reason}, [f"context={pin}"])
    env = {**os.environ, "ROUTE_LAST_CLASS": cls}
    facts = supplied_facts(task)
    if facts:
        env["ROUTE_FACTS"] = facts
    r = subprocess.run([str(LAUNCH), "worker", task, t["owner"], str(path)], capture_output=True, text=True, env=env)
    log(f"relaunch {task}: rc={r.returncode} {r.stdout.strip()} {r.stderr.strip()[-300:]}")
    if r.returncode != 0:
        emit("task.escalated", task, {"to": "chief_of_staff", "code": "other",
                                      "reason": f"restart launch failed (rc {r.returncode}): {r.stderr.strip()[-200:]}"})


def tick(dry: bool) -> None:
    state = json.loads(hive("state"))
    now = time.time()
    for t in state.get("tasks", {}).values():
        ext = t.get("ext") or {}
        if ext.get("escalation") or t["status"] not in ("assigned", "in_progress", "in_review"):
            continue
        task = t["id"]
        if t["status"] == "in_review":
            rv = t.get("assigned_reviewer")
            att = latest_attempt(task, "review")
            if rv and not t.get("latest_review") and att and not alive(att[1]):
                hist = json.loads(hive("task", task))["events"]
                assigned = max((ts(e["recorded_at"]) for e in hist if e["type"] == "review.assigned"), default=0)
                pid_time = (ROUTE / task / f"review.{att[0]}.pid").stat().st_mtime
                if pid_time >= assigned - 60:   # this review's process, not an older one
                    emit("task.escalated", task, {"to": "chief_of_staff", "code": "other",
                                                  "reason": f"reviewer {rv}'s process ended without a verdict"}, dry=dry)
            continue
        every = (ext.get("lease") or {}).get("checkin_every_seconds")
        if not every:
            continue
        att = latest_attempt(task, "worker")
        if att is None:
            continue   # launched by hand, not by the launcher: nothing to act on
        n, pid = att
        last = ts((t.get("last_activity") or {}).get("at")) or ts(t.get("assigned_at")) or now
        pid_start = (ROUTE / task / f"worker.{n}.pid").stat().st_mtime
        since = max(last, pid_start)
        attempt = ext.get("attempt", 1)
        if not alive(pid):
            if now - pid_start < 120:
                continue
            if attempt >= MAX_ATTEMPTS:
                emit("task.escalated", task, {"to": "chief_of_staff", "code": "repeated_failure",
                                              "reason": f"{attempt} attempts; the last worker process ended without a result"}, dry=dry)
                continue
            hist = json.loads(hive("task", task))["events"]
            cls = failure_class(task, "worker", n)
            why = ("the worker hit a usage or quota limit" if cls == "capacity"
                   else "the worker's process ended without posting a result")
            if (t.get("latest_review") or {}).get("verdict") in ("failed", "needs_information"):
                cls, why = "failed_check", "the independent review sent the result back"
            restart(t, hist, why, cls, dry)
            continue
        if now - since <= every + GRACE:
            continue
        nudges = ext.get("nudges_since_activity", 0)
        if nudges < MAX_NUDGES:
            hist = json.loads(hive("task", task))["events"]
            last_nudge = max((ts(e["recorded_at"]) for e in hist if e["type"] == "task.nudged"), default=0)
            if now - last_nudge > every:
                emit("task.nudged", task, {"reason": f"no activity for {int((now - since) / 60)} min "
                                                     f"(check-in every {every // 60} min); worker process alive"}, dry=dry)
            continue
        if attempt >= MAX_ATTEMPTS:
            emit("task.escalated", task, {"to": "chief_of_staff", "code": "stuck",
                                          "reason": f"silent after {nudges} nudges and {attempt} attempts"}, dry=dry)
            continue
        hist = json.loads(hive("task", task))["events"]
        if not dry:
            stop(pid)
            time.sleep(5)
        restart(t, hist, f"silent for {int((now - since) / 60)} min after {nudges} nudges", "stalled", dry)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    setup()
    log(f"supervisor up (interval {a.interval}s{', dry run' if a.dry_run else ''})")
    while True:
        try:
            tick(a.dry_run)
        except Exception as e:   # one bad tick must not stop supervision
            log(f"tick failed: {e}")
        if a.once:
            return
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main())
