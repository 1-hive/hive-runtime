#!/usr/bin/env python3
"""escalation_hold: an open escalation holds a task from the dispatcher, except a result posted after it,
which is reviewed anyway (proto-hive F3, 2026-11-08: a "stuck" escalation at 02:12 held the 03:28 result
for 13 hours, silently, until the chief of staff resolved it and assigned the review by hand)."""
import importlib.util, os, sys, tempfile
from pathlib import Path

os.environ.pop("HIVE_LONG_LIVED_ACTORS", None)
os.environ.pop("HIVE_REVIEW_ROUTE_TABLE", None)
os.environ["HIVE_WORK_DIR"] = tempfile.mkdtemp()
HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("dispatcher", HERE / "dispatcher.py")
dis = importlib.util.module_from_spec(spec); sys.modules["dispatcher"] = dis; spec.loader.exec_module(dis)
dis.log = lambda m: None

STUCK = {"code": "stuck", "to": "chief_of_staff", "position": 1497}
RESULT = {"event_id": "r1", "author": "worker.protocosmo2", "position": 1505}
f3 = {"id": "f3", "status": "in_review", "owner": "worker.protocosmo2", "current_result": RESULT,
      "ext": {"escalation": STUCK}}

assert not dis.held(f3), "a result posted after the escalation answers it"
assert dis.held({**f3, "ext": {"escalation": {**STUCK, "position": 1506}}}), \
    "an escalation after the result (about the result) still holds it"
assert dis.held({**f3, "status": "in_progress", "current_result": None}), "no result yet: held"
assert not dis.held({**f3, "ext": {}}), "no escalation: not held"

# needs_review and handle(): F3's result is dispatched with the stuck escalation still open.
assert dis.needs_review(f3, [])
dispatched = []
dis.dispatch = lambda t, state, dry, retry_at, events: dispatched.append(t["id"])
dis.hive = lambda *a: '{"events": []}'
dis.handle(f3, {}, False, {})
assert dispatched == ["f3"], dispatched
dis.handle({**f3, "ext": {"escalation": {**STUCK, "position": 1506}}}, {}, False, {})
assert dispatched == ["f3"], "held: not dispatched"
print("PASSED")
