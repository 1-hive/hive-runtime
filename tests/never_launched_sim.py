#!/usr/bin/env python3
"""A worker task the dispatcher can't launch, against a stubbed record.

    python3 tests/never_launched_sim.py

Checks: a launcher refusal (an order's bad Route facts line) is kept by the dispatcher and escalated
by the supervisor to the chief of staff at once, with the launcher's reason; not again until after the
escalation is resolved and the dispatcher is refused again; a launch that works clears it; with no
refusal, an assigned task with no attempt is escalated only past its acceptance window.
"""
import importlib.util, json, os, sys, tempfile, time
from pathlib import Path

os.environ.pop("HIVE_LONG_LIVED_ACTORS", None)
os.environ["HIVE_WORK_DIR"] = tempfile.mkdtemp()
HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m; spec.loader.exec_module(m); return m


sup = load("supervisor"); dis = load("dispatcher")
NOW = time.time(); iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
emitted, rc = [], [2]
sup.log = dis.log = lambda m: None
sup.emit = lambda etype, task, data, refs=(), dry=False, on="task": emitted.append((etype, task, data))
EVENTS = {}
sup.hive = dis.hive = lambda *a: json.dumps({"events": EVENTS.get(a[1], [])})
REFUSAL = "Route facts: bad item 'specification=clear'\nfix the order's Route facts line"
dis.subprocess.run = lambda *a, **k: type("R", (), {"returncode": rc[0], "stdout": "", "stderr": REFUSAL if rc[0] else ""})()


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f": {detail}")); cond or sys.exit(1)


lease = {"accept_within_seconds": 1800, "checkin_every_seconds": 1800}
t = {"id": "t1", "status": "assigned", "owner": "worker.claude.1", "assigned_at": iso(NOW - 120), "title": "x",
     "goal": "g", "project": "p", "order": {"path": "o.md"}, "current_result": None, "ext": {"lease": lease}}
state = {"tasks": {"t1": t}, "goals": {}}

dis.handle(t, state, False, {})
kept = json.loads(dis.REFUSED.read_text()).get("t1")
check("dispatcher keeps the refusal", kept and kept["rc"] == 2 and "specification=clear" in kept["reason"], kept)
sup.supervise(t, NOW, False)
check("supervisor escalates it to the chief of staff at once, with the reason",
      len(emitted) == 1 and emitted[0][2]["to"] == "chief_of_staff" and emitted[0][2]["code"] == "stuck"
      and "specification=clear" in emitted[0][2]["reason"] and len(emitted[0][2]["reason"]) <= 500, emitted)

EVENTS["t1"] = [{"type": "task.escalated", "recorded_at": iso(kept["at"] + 5)},
                {"type": "task.escalation_resolved", "recorded_at": iso(kept["at"] + 60)}]
emitted.clear(); sup.supervise(t, kept["at"] + 90, False)
check("resolved, no new refusal yet: nothing", not emitted, emitted)
dis.time.time = lambda: kept["at"] + 300   # the dispatcher's next retry
dis.handle(t, state, False, {}); sup.supervise(t, kept["at"] + 330, False)
check("refused again after the resolution: escalated again", len(emitted) == 1, emitted)

rc[0] = 0; dis.handle(t, state, False, {})
check("a launch that works clears the refusal", "t1" not in json.loads(dis.REFUSED.read_text()))

EVENTS["t1"] = []; emitted.clear(); sup.supervise(t, NOW, False)
check("no refusal, inside the acceptance window: nothing", not emitted, emitted)
t["assigned_at"] = iso(NOW - 1800 - sup.GRACE - 60); sup.supervise(t, NOW, False)
check("no refusal, past the window: escalated, never launched",
      len(emitted) == 1 and "never launched" in emitted[0][2]["reason"], emitted)
print("PASSED")
