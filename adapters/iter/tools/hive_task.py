import json
import os
import time
from pathlib import Path

DESCRIPTION = ("Act on a hive record task (the hive channel tells you of them). action: accept (when you start), "
               "progress or checkpoint (text = done and next), block (text = your question; extra = decision, "
               "information, access or external), unblock (once answered), result (text = the report; push code "
               "first), review (a task you review; text = findings; extra = passed, failed or needs_information). "
               "task = the task id (empty: your only open task). The outcome comes on the hive channel within a "
               "minute.")

ACTIONS = ("accept", "progress", "checkpoint", "block", "unblock", "result", "review")


def run(action, task, text, extra=""):
    action = (action or "").strip().lower()
    if action not in ACTIONS:
        return f"FAILED: action must be one of {', '.join(ACTIONS)}"
    root = Path(__file__).resolve().parent.parent
    if not (root / ".hive" / "config.json").is_file():
        return "FAILED: the hive channel isn't configured (no .hive/config.json)"
    out = root / ".runtime" / "hive" / "outbox"
    out.mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns()}-{os.getpid()}"
    tmp = out / f".{name}.tmp"
    tmp.write_text(json.dumps({"action": action, "task": (task or "").strip(), "text": text or "",
                               "extra": (extra or "").strip()}))
    tmp.replace(out / name)
    return f"QUEUED: {action} {task or '(your open task)'}; the outcome arrives on the hive channel"
