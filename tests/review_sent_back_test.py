#!/usr/bin/env python3
"""review_sent_back: one failed review restarts the worker once as a failed check. A later attempt that
ends without a result is classed by how it ended, not as another failed check (1-hive ref-2-ladder,
2026-11-05: one failed review, then three restarts all called "the independent review sent the result
back", which moved the route up two tiers and used up the attempts in 16 minutes)."""
import importlib.util, os, sys, tempfile, time
from pathlib import Path

os.environ.pop("HIVE_LONG_LIVED_ACTORS", None)
os.environ["HIVE_WORK_DIR"] = tempfile.mkdtemp()
HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("supervisor", HERE / "supervisor.py")
sup = importlib.util.module_from_spec(spec); sys.modules["supervisor"] = sup; spec.loader.exec_module(sup)
sup.log = lambda m: None

FAILED = {"verdict": "failed", "position": 1430}
ev = lambda pos, typ: {"position": pos, "type": typ}
REVIEW = [ev(1427, "task.result_posted"), ev(1430, "review.recorded")]

# The unit: a failed review counts until the supervisor restarts the worker for it, and not after.
assert sup.review_sent_back({"latest_review": FAILED}, REVIEW)
assert not sup.review_sent_back({"latest_review": FAILED}, REVIEW + [ev(1431, "task.restarted")])
assert not sup.review_sent_back({"latest_review": {"verdict": "passed", "position": 1430}}, REVIEW)
assert not sup.review_sent_back({"latest_review": None}, [])
assert sup.review_sent_back({"latest_review": {"verdict": "needs_information", "position": 9}},
                            [ev(5, "task.restarted"), ev(9, "review.recorded")])

# Through supervise(): a dead worker, its task in progress, the record's latest review failed.
NOW = time.time()
restarts, checkpoints_taken = [], []
TASK = "ref-2-ladder"
(sup.ROUTE / TASK).mkdir(parents=True, exist_ok=True)
pid = sup.ROUTE / TASK / "worker.1.pid"
pid.write_text("999999")
os.utime(pid, (NOW - 600, NOW - 600))
sup.latest_attempt = lambda task, role: (1, 999999)
sup.alive = lambda p: False
sup.failure_class = lambda task, role, n: "outage"
sup.checkpoints = lambda task: []
sup.take_checkpoint = lambda t, n, start, hist, dry: checkpoints_taken.append(n) or None
sup.restart = lambda t, hist, why, cls, dry: restarts.append((cls, why))
sup.emit = lambda *a, **k: None
sup.handled = lambda task, esc: True
HIST = []
sup.hive = lambda *a: __import__("json").dumps({"events": HIST})
task = {"id": TASK, "status": "in_progress", "owner": "worker.claude.1", "latest_review": FAILED,
        "ext": {"lease": {"checkin_every_seconds": 1800}, "attempt": 2}}

HIST[:] = REVIEW
sup.supervise(task, NOW, dry=False)
assert restarts[-1][0] == "failed_check" and not checkpoints_taken, (restarts, checkpoints_taken)

HIST[:] = REVIEW + [ev(1431, "task.restarted"), ev(1434, "task.reported")]
sup.supervise(task, NOW, dry=False)
assert checkpoints_taken == [1], "the attempt after the restart is checked for a checkpoint"
assert restarts[-1] == ("outage", "the worker's process ended without posting a result"), restarts[-1]
print("PASSED")
