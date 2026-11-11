#!/usr/bin/env python3
"""`op restart <task> [note]`: the operator's fresh attempt, for an escalated task or a running one. The operator resolves the
escalation with "Restart: <note>"; the supervisor restarts the worker (stopping it first if it still runs), once,
and counts attempts again from that restart. A chief of staff's resolution with the same words doesn't count.
Before this a task escalated as repeated_failure had no way back: resolving it got it escalated again at once."""
import datetime as dt, importlib.util, json, os, sys, tempfile, time
from pathlib import Path

os.environ.pop("HIVE_LONG_LIVED_ACTORS", None)
os.environ["HIVE_WORK_DIR"] = tempfile.mkdtemp()
HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("supervisor", HERE / "supervisor.py")
sup = importlib.util.module_from_spec(spec); sys.modules["supervisor"] = sup; spec.loader.exec_module(sup)
sup.log = lambda msg: None

NOW = time.time()
iso = lambda s_ago: dt.datetime.fromtimestamp(NOW - s_ago, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
OPERATOR, COS = {"class": "operator", "id": "operator"}, {"class": "chief_of_staff", "id": "cos"}


def ev(pos, typ, ago=0, actor=None, **data):
    return {"position": pos, "type": typ, "recorded_at": iso(ago), "actor": actor or {"class": "supervisor"},
            "data": data}


HIST = []
sup.hive = lambda *a, **k: json.dumps({"events": HIST})
TASK = "t1"
(sup.ROUTE / TASK).mkdir(parents=True, exist_ok=True)
pid = sup.ROUTE / TASK / "worker.3.pid"
pid.write_text("999999")
os.utime(pid, (NOW - 3600, NOW - 3600))
sup.latest_attempt = lambda task, role: (3, 999999)
sup.failure_class = lambda task, role, n: "interrupted"
sup.take_checkpoint = lambda *a, **k: None
ALIVE = [False]
sup.alive = lambda p: ALIVE[0]
stopped, restarts, emitted = [], [], []
sup.stop = lambda p, c=None: stopped.append(p)
sup.time.sleep = lambda s: None
sup.restart = lambda t, hist, reason, cls, dry: restarts.append((reason, cls))
sup.emit = lambda typ, task, data, *a, **k: emitted.append((typ, data))

t = {"id": TASK, "status": "in_progress", "owner": "worker.claude.1", "title": "x",
     "last_activity": {"position": 9, "at": iso(3600)},
     "latest_review": {"verdict": "failed", "position": 8},
     "ext": {"lease": {"checkin_every_seconds": 1800}, "attempt": 4, "escalation": None}}
BEFORE = [ev(1, "task.assigned", 9000), ev(2, "task.restarted", 8000), ev(3, "task.restarted", 7000),
          ev(4, "task.restarted", 6000), ev(7, "task.result_posted", 5000),
          ev(8, "review.recorded", 4000, verdict="failed"), ev(9, "task.escalated", 3600, code="repeated_failure")]

# Without the operator's request: the attempts are used up, so it is escalated (again), as before.
HIST[:] = BEFORE + [ev(10, "task.escalation_resolved", 60, COS, resolution="Restart: fix finding 2")]
sup.supervise(t, NOW, dry=False)
assert not restarts and emitted and emitted[0][1]["code"] == "repeated_failure", (restarts, emitted)
assert "op restart t1" in emitted[0][1]["reason"], emitted
emitted.clear()

# The operator's request: one restart, with the note; the failed review that sent it back sets the class.
REQ = ev(10, "task.escalation_resolved", 60, OPERATOR, resolution="Restart: fix review 4 findings 1-3 only")
HIST[:] = BEFORE + [REQ]
assert sup.restart_request(HIST) is REQ
sup.supervise(t, NOW, dry=False)
assert restarts == [("the operator asked for a fresh attempt: fix review 4 findings 1-3 only", "failed_check")], restarts
assert not emitted and not stopped
restarts.clear()

# Restarted: not again, and the count starts from that restart (attempt 5 is the new attempt 1).
HIST[:] = BEFORE + [REQ, ev(11, "task.restarted", 30)]
t5 = {**t, "ext": {**t["ext"], "attempt": 5}}
assert sup.attempts_used(t5, HIST) == 1
os.utime(pid, (NOW - 600, NOW - 600))
sup.supervise(t5, NOW, dry=False)   # that attempt's worker ended without a result: restarted, not escalated
assert len(restarts) == 1 and "ended without posting a result" in restarts[0][0] and not emitted, (restarts, emitted)
restarts.clear()
t8 = {**t, "ext": {**t["ext"], "attempt": 8}}
HIST[:] = BEFORE + [REQ] + [ev(11 + i, "task.restarted", 30 - i) for i in range(4)]
assert sup.attempts_used(t8, HIST) == 4
sup.supervise(t8, NOW, dry=False)
assert not restarts and emitted[0][1]["code"] == "repeated_failure", "a full budget, then escalated again"
emitted.clear()

# A worker still running (escalated as stuck): stopped first, then restarted.
ALIVE[0] = True
HIST[:] = BEFORE + [ev(10, "task.escalation_resolved", 60, OPERATOR, resolution="Restart:")]
sup.supervise(t, NOW, dry=False)
assert stopped == [999999] and restarts == [("the operator asked for a fresh attempt", "interrupted")], (stopped, restarts)

# Checkpoints after the request don't count; those before it are among the restarts already counted.
(sup.ROUTE / TASK / "checkpoints.jsonl").write_text(
    json.dumps({"attempt": 1, "at": iso(7500)[:19] + "Z"}) + "\n" + json.dumps({"attempt": 6, "at": iso(10)[:19] + "Z"}) + "\n")
HIST[:] = BEFORE + [REQ] + [ev(11 + i, "task.restarted", 30 - i) for i in range(4)]
assert sup.attempts_used(t8, HIST) == 3
HIST[:] = BEFORE
assert sup.attempts_used(t, HIST) == 2, "no request: attempt minus every checkpoint, as before"
(sup.ROUTE / TASK / "checkpoints.jsonl").unlink()

# A request without an escalation (op restart on a running task, e.g. after a new route override): the worker is
# stopped and restarted once, and the record says the operator asked; the request is used up.
REQF = sup.ROUTE / TASK / sup.RESTART_FILE
t1 = {**t, "latest_review": None, "ext": {**t["ext"], "attempt": 1}}
HIST[:] = [ev(1, "task.assigned", 9000)]
ALIVE[0] = True; stopped.clear(); restarts.clear()
REQF.write_text(json.dumps({"at": NOW - 30, "note": "on Opus now", "by": "operator"}))
assert sup.host_restart_request(TASK)["note"] == "on Opus now"
sup.supervise(t1, NOW, dry=False)
assert stopped == [999999] and restarts == [("the operator asked for a fresh attempt: on Opus now", "interrupted")], \
    (stopped, restarts)
assert not REQF.exists() and not emitted
restarts.clear(); stopped.clear()
# Its restart starts the count again, as an op restart after an escalation does.
HIST[:] = [ev(1, "task.assigned", 9000), ev(2, "task.restarted", 8000, reason="the worker's process ended"),
           ev(3, "task.restarted", 20, reason="the operator asked for a fresh attempt: on Opus now")]
assert sup.attempts_used({**t, "ext": {**t["ext"], "attempt": 3}}, HIST) == 1
# A request older than the attempt that runs now is stale: dropped, nothing stopped.
REQF.write_text(json.dumps({"at": NOW - 7200, "note": ""}))
sup.supervise(t1, NOW, dry=False)
assert not REQF.exists() and not stopped and not restarts, (stopped, restarts)
# A worker that ended on its own: the reason says how it ended, not only that it posted no result.
ALIVE[0] = False
HIST[:] = [ev(1, "task.assigned", 9000)]
sup.supervise(t1, NOW, dry=False)
assert len(restarts) == 1 and restarts[0][0].startswith("the worker's process ended without posting a result: "), restarts
restarts.clear()

# --- op itself, against a stand-in hive -------------------------------------------------------------------------
import subprocess
B = Path(tempfile.mkdtemp())
STATE, ARGS = B / "task.json", B / "args"
(B / "hive").write_text(f"""#!/bin/sh
if [ "$1" = task ]; then cat {STATE}; exit 0; fi
printf '%s\\n' "$@" > {ARGS}
echo '{{"task": "t1", "position": 42}}'
""")
(B / "hive").chmod(0o755)
OP = lambda *a: subprocess.run([str(HERE / "op"), *a], capture_output=True, text=True,
                               env={**os.environ, "HIVE": str(B / "hive")})
ESC = {"code": "repeated_failure", "position": 9}
STATE.write_text(json.dumps({"task": {"id": "t1", "status": "in_progress", "ext": {"escalation": ESC}}}))
r = OP("restart", "t1", 'fix "finding" 2')
assert r.returncode == 0 and "position 42" in r.stdout, r
a = ARGS.read_text().splitlines()
assert a[:4] == ["emit", "task.escalation_resolved", "--task", "t1"], a
assert json.loads(a[a.index("--data") + 1]) == {"resolution": 'Restart: fix "finding" 2'}, a
ARGS.unlink()
r = OP("restart", "t1")
assert r.returncode == 0 and json.loads(ARGS.read_text().splitlines()[-1]) == {"resolution": "Restart: "}, r
# No open escalation: the request goes to the supervisor (supervisor.RESTART_FILE), nothing to the record.
STATE.write_text(json.dumps({"task": {"id": "t1", "status": "in_progress", "ext": {"escalation": None}}}))
ARGS.unlink(missing_ok=True)
r = OP("restart", "t1", "move to Opus")
assert r.returncode == 0 and "restart requested" in r.stdout and not ARGS.exists(), r
d = json.loads(REQF.read_text())
assert d["note"] == "move to Opus" and d["by"] == "operator" and abs(d["at"] - time.time()) < 60, d
REQF.unlink()
for p_ in (sup.ROUTE / TASK).glob("worker.*.pid"):
    p_.unlink()
for task, why in (({"id": "t1", "status": "in_progress", "ext": {"escalation": None}}, "no worker attempt"),
                  ({"id": "t1", "status": "blocked", "ext": {"escalation": ESC}}, "is blocked")):
    STATE.write_text(json.dumps({"task": task}))
    r = OP("restart", "t1")
    assert r.returncode != 0 and why in r.stderr, (why, r)
assert OP("restart").returncode == 2

print("op_restart test: OK")
