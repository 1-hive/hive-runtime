#!/usr/bin/env python3
"""R5 in the dispatcher and supervisor:
- a review asked for again after a verdict on the same result (`op review`, the chief of staff) is launched;
- a worker that waited in its session for its review is not nudged for that wait: after a verdict it hasn't
  acted on yet, its silence counts from the verdict;
- a reviewer that dies on such a second review is escalated like any other."""
import datetime as dt, importlib.util, json, os, sys, tempfile, time
from pathlib import Path

os.environ.pop("HIVE_LONG_LIVED_ACTORS", None)
os.environ.pop("HIVE_REVIEW_ROUTE_TABLE", None)
os.environ["HIVE_WORK_DIR"] = tempfile.mkdtemp()
HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m; spec.loader.exec_module(m)
    m.log = lambda msg: None
    return m


sup = load("supervisor")
dis = load("dispatcher")
NOW = time.time()
iso = lambda s_ago: dt.datetime.fromtimestamp(NOW - s_ago, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
ev = lambda pos, typ, ago=0, **data: {"position": pos, "type": typ, "recorded_at": iso(ago), "data": data}

# --- dispatcher: needs_review -------------------------------------------------------------------------------
RES = {"event_id": "r1", "author": "worker.claude.1", "position": 10}
t = {"id": "t1", "status": "in_review", "owner": "worker.claude.1", "current_result": RES, "ext": {}}
asg = lambda pos, ago=0: ev(pos, "review.assigned", ago, reviewer="reviewer.codex.1", result_event="r1")
rec = lambda pos, v="passed": ev(pos, "review.recorded", 0, verdict=v, result_event="r1")
assert dis.needs_review(t, []), "no review yet"
assert not dis.needs_review(t, [asg(11, 300), rec(12)]), "reviewed, nothing asked since"
# No reviewer process since the new assignment: the dispatcher launches it.
assert dis.needs_review(t, [asg(11, 900), rec(12), asg(13, 60)]), "asked again after the verdict"
assert not dis.needs_review(t, [asg(11, 900), rec(12), asg(13, 60), rec(14)]), "and reviewed again"
other = lambda pos: ev(pos, "review.recorded", 0, verdict="failed", result_event="r0")
assert dis.needs_review(t, [other(9)]), "a verdict on an older result doesn't count"

# --- supervisor: verdict_is_recent, reviewed_since_assigned --------------------------------------------------
HIST = []
sup.hive = lambda *a, **k: json.dumps({"events": HIST})
EVERY = 1800
w = {"id": "t1", "status": "in_progress", "owner": "worker.claude.1",
     "last_activity": {"position": 10, "at": iso(3600)},
     "latest_review": {"verdict": "failed", "position": 12},
     "ext": {"lease": {"checkin_every_seconds": EVERY}, "attempt": 1}}
HIST[:] = [ev(10, "task.result_posted", 3600), asg(11, 3500), ev(12, "review.recorded", 120, verdict="failed")]
assert sup.verdict_is_recent(w, NOW, EVERY), "verdict 2 min ago: not silent yet"
HIST[-1] = ev(12, "review.recorded", 3000, verdict="failed")
assert not sup.verdict_is_recent(w, NOW, EVERY), "verdict 50 min ago, nothing since"
assert not sup.verdict_is_recent({**w, "last_activity": {"position": 13}}, NOW, EVERY), "the worker acted since"
assert not sup.verdict_is_recent({**w, "latest_review": None}, NOW, EVERY)

assert sup.reviewed_since_assigned([asg(11), rec(12)])
assert not sup.reviewed_since_assigned([asg(11), rec(12), asg(13)])
assert not sup.reviewed_since_assigned([asg(11)])

# --- supervisor: supervise() on a live worker after a long review ---------------------------------------------
TASK = "t1"
(sup.ROUTE / TASK).mkdir(parents=True, exist_ok=True)
pid = sup.ROUTE / TASK / "worker.1.pid"
pid.write_text("999999")
os.utime(pid, (NOW - 7200, NOW - 7200))
nudged, escalated = [], []
sup.latest_attempt = lambda task, role: (1, 999999)
sup.alive = lambda p: True
sup.handled = lambda task, esc: True
sup.emit = lambda typ, task, data, *a, **k: (nudged if typ == "task.nudged" else escalated).append(data)
HIST[:] = [ev(10, "task.result_posted", 3600), asg(11, 3500), ev(12, "review.recorded", 120, verdict="failed")]
sup.supervise(w, NOW, dry=False)
assert not nudged, "the worker waited for its review in its session; the verdict is 2 min old"
HIST[-1] = ev(12, "review.recorded", 3000, verdict="failed")
sup.supervise(w, NOW, dry=False)
assert len(nudged) == 1, "50 min after the verdict with nothing done: nudged as before"

# --- supervisor: a dead reviewer on a review asked for again ---------------------------------------------------
nudged.clear(); escalated.clear()
r = {"id": TASK, "status": "in_review", "owner": "worker.claude.1", "assigned_reviewer": "reviewer.codex.1",
     "current_result": RES, "latest_review": {"verdict": "passed", "position": 12}, "ext": {}}
(sup.ROUTE / f"{TASK}-review").mkdir(parents=True, exist_ok=True)
rp = sup.ROUTE / f"{TASK}-review" / "review.2.pid"
rp.write_text("999998")
sup.latest_attempt = lambda task, role: (2, 999998)
sup.alive = lambda p: False
sup.failure_class = lambda task, role, n: "unknown"
sup.end_reason = lambda task, role, n: "unknown"
HIST[:] = [asg(11, 900), rec(12), asg(13, 60)]
os.utime(rp, (NOW - 30, NOW - 30))
sup.supervise(r, NOW, dry=False)
assert escalated and "without a verdict" in escalated[0]["reason"], escalated
escalated.clear()
HIST[:] = [asg(11, 900), rec(12), asg(13, 60), rec(14)]
sup.supervise(r, NOW, dry=False)
assert not escalated, "the second review was recorded"

print("review_in_session test: OK")
