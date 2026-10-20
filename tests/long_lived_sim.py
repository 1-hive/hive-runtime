"""Long-lived mode of the supervisor and dispatcher, against a stubbed record.

    HIVE_LONG_LIVED_ACTORS='worker.*,reviewer.astra' python3 tests/long_lived_sim.py

Checks: no launch and no restart for long-lived actors; nudge, nudge, escalate; a review assigned
but not recorded within the wait is escalated; a review is assigned once (never re-assigned).
"""
import importlib.util, json, os, sys, tempfile, time
from pathlib import Path

os.environ.setdefault("HIVE_LONG_LIVED_ACTORS", "worker.*,reviewer.astra")
os.environ["HIVE_WORK_DIR"] = tempfile.mkdtemp()
os.environ["HIVE_REVIEWERS"] = "reviewer.astra"
HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m; spec.loader.exec_module(m); return m


sup = load("supervisor"); dis = load("dispatcher")
NOW = time.time(); iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
emitted, launched = [], []
sup.log = dis.log = lambda m: None
sup.emit = lambda etype, task, data, refs=(), dry=False, on="task": emitted.append((etype, task, data))
sup.restart = lambda *a, **k: emitted.append(("RESTART",))
sup.stop = lambda *a, **k: emitted.append(("STOP",))
EVENTS = {}
def fake_hive(*a):
    if a[0] == "task": return json.dumps({"events": EVENTS.get(a[1], [])})
    if a[0] == "emit": emitted.append((a[1], a[3], json.loads(a[a.index("--data") + 1]))); return json.dumps({"position": 1})
    raise AssertionError(a)
sup.hive = dis.hive = fake_hive
dis.subprocess.run = lambda *a, **k: launched.append(a[0]) or type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f": {detail}")); cond or sys.exit(1)


lease = {"accept_within_seconds": 600, "checkin_every_seconds": 900}
t = {"id": "t1", "status": "assigned", "owner": "worker.protomegabot2", "assigned_at": iso(NOW - 2000),
     "ext": {"lease": lease, "nudges_since_activity": 0}, "title": "x", "order": {"path": "o.md"}, "current_result": None}
sup.supervise(t, NOW, False)
check("late acceptance: first nudge", emitted[-1][0] == "task.nudged" and "not accepted" in emitted[-1][2]["reason"], emitted)
EVENTS["t1"] = [{"type": "task.nudged", "recorded_at": iso(NOW - 10)}]
t["ext"]["nudges_since_activity"] = 1; emitted.clear(); sup.supervise(t, NOW, False)
check("a fresh nudge isn't repeated", not emitted, emitted)
EVENTS["t1"] = [{"type": "task.nudged", "recorded_at": iso(NOW - 1000)}]; sup.supervise(t, NOW, False)
check("second nudge after the interval", emitted and emitted[-1][0] == "task.nudged", emitted)
t["ext"]["nudges_since_activity"] = 2; emitted.clear(); sup.supervise(t, NOW, False)
check("then escalate to the chief of staff, never restart", emitted == [("task.escalated", "t1", emitted[0][2])]
      and emitted[0][2]["to"] == "chief_of_staff" and emitted[0][2]["code"] == "stuck", emitted)
t.update(status="in_progress", last_activity={"at": iso(NOW - 100)}); t["ext"]["nudges_since_activity"] = 0; emitted.clear()
sup.supervise(t, NOW, False); check("active worker: nothing", not emitted, emitted)
r = {"id": "t2", "status": "in_review", "owner": "worker.protomegabot2", "assigned_reviewer": "reviewer.astra",
     "latest_review": None, "current_result": {"event_id": "e1"}, "ext": {"lease": lease}, "title": "y"}
EVENTS["t2"] = [{"type": "review.assigned", "recorded_at": iso(NOW - 3600), "data": {"reviewer": "reviewer.astra", "result_event": "e1"}}]
emitted.clear(); sup.supervise(r, NOW, False); check("review waiting under the limit: nothing", not emitted, emitted)
EVENTS["t2"][0]["recorded_at"] = iso(NOW - 8000); sup.supervise(r, NOW, False)
check("review waiting past the limit: escalated", emitted and emitted[-1][2]["code"] == "other" and "no verdict" in emitted[-1][2]["reason"], emitted)
# dispatcher
state = {"tasks": {}, "goals": {}}
emitted.clear(); launched.clear()
dis.handle({**t, "status": "assigned"}, state, False, {}); check("dispatcher: a long-lived worker is never launched", not launched and not emitted, (launched, emitted))
r2 = {**r, "assigned_reviewer": None}; EVENTS["t2"] = []
dis.handle(r2, state, False, {})
check("dispatcher: review assigned, cause = the result, no launch", emitted and emitted[0][0] == "review.assigned"
      and emitted[0][2]["reviewer"] == "reviewer.astra" and not launched, (emitted, launched))
EVENTS["t2"] = [{"type": "review.assigned", "recorded_at": iso(NOW), "data": {"reviewer": "reviewer.astra", "result_event": "e1"}}]
emitted.clear(); dis.handle({**r2, "assigned_reviewer": "reviewer.astra"}, state, False, {})
check("dispatcher: an assigned long-lived review is never re-assigned", not emitted and not launched, emitted)
print("PASSED")
