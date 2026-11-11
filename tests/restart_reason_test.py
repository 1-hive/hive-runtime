#!/usr/bin/env python3
"""A restart's reason fits the record (maxLength 500), however long the operator's note. Before this, an `op restart`
note over 460 characters (op allows 500; the supervisor adds "the operator asked for a fresh attempt: ") was refused
as SCHEMA_INVALID on every tick, with the worker already stopped (1-hive value-1, 2026-11-11). The restart context
keeps the whole note."""
import importlib.util, os, sys, tempfile, types
from pathlib import Path

os.environ.pop("HIVE_LONG_LIVED_ACTORS", None)
os.environ["HIVE_WORK_DIR"] = tempfile.mkdtemp()
HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("supervisor", HERE / "supervisor.py")
sup = importlib.util.module_from_spec(spec); sys.modules["supervisor"] = sup; spec.loader.exec_module(sup)
sup.log = lambda msg: None

sup.WS = Path(tempfile.mkdtemp())
sup.HOME = Path(tempfile.mkdtemp())
sup.git = lambda *a: ""
sup.route_env = lambda task: {}
emitted, runs = [], []
sup.emit = lambda typ, task, data, *a, **k: emitted.append((typ, data))
sup.subprocess = types.SimpleNamespace(
    run=lambda argv, **k: runs.append(argv) or types.SimpleNamespace(returncode=0, stdout="", stderr=""))

t = {"id": "t1", "owner": "worker.claude.1", "title": "x", "goal": "g", "project": "p",
     "order": {"path": "projects/p/orders/t1.md"}, "ext": {"attempt": 1}}
note = "n" * 500   # the longest note op passes on
sup.restart(t, [], sup.OPERATOR_RESTART + ": " + note, "interrupted", dry=False)

typ, data = emitted[0]
assert typ == "task.restarted" and len(data["reason"]) == sup.REASON_MAX == 500, (typ, len(data["reason"]))
assert data["reason"].startswith(sup.OPERATOR_RESTART + ": nnn"), "still marked as the operator's restart"
body = (sup.WS / "projects/p/runs/t1/restart-2.md").read_text()
assert note in body, "the restart context keeps the whole note"
assert runs and runs[-1][1:3] == ["worker", "t1"], runs

# A short reason is untouched.
emitted.clear()
sup.restart(t, [], "the worker's process ended", "interrupted", dry=False)
assert emitted[0][1]["reason"] == "the worker's process ended", emitted

print("restart_reason test: OK")
