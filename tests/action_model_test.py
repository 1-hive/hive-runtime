#!/usr/bin/env python3
"""Record A7 for long-lived agents (adapters/iter/channels/hive.py): the hive channel's daemon names the model that
wrote a question, report, result or review. Iter's send() carries Iter's own "[model]" line (taken off the report);
a hive_task action takes the model of the route-hook episode open when it was queued; an OpenClaw agent's, the newest
assistant message in its session database. Nothing found: no model. The flag goes only to a hive CLI that has it.

    python3 tests/action_model_test.py
"""
import importlib.util
import json
import sqlite3
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("hive_channel", ROOT / "adapters/iter/channels/hive.py")
ch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ch)


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


class H:
    def __init__(self, beacon=None):
        self.c = {"beacon": beacon or {}}


with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    ch.configure(d, d / ".hive" / "config.json")
    now = 1_800_000_000

    # Iter's send(): its own "[model]" line, the exact model of that response
    m, text = ch.action_model(H(), {"action": "progress", "via": "send", "text": ""}, "[claude-opus-5-5]\nHalfway.")
    assert (m, text) == ("claude-opus-5-5", "Halfway."), (m, text)
    # the same line from hive_task (not send) is the agent's text, not a tag
    assert ch.action_model(H(), {"action": "result", "text": ""}, "[x]\nbody")[0] is None
    assert ch.action_model(H(), {"model": "gpt-5.5"}, "t") == ("gpt-5.5", "t")

    # the route hook: the episode open when the action was queued
    hook = d / "route-hook.jsonl"
    hook.write_text("\n".join(json.dumps(x) for x in (
        {"at": iso(now - 600), "event": "routed", "episode": "e1", "model": "anthropic/claude-sonnet-5-5"},
        {"at": iso(now - 500), "event": "ended", "episode": "e1", "models": ["anthropic/claude-sonnet-5-5"]},
        {"at": iso(now - 100), "event": "routed", "episode": "e2", "model": "anthropic/claude-opus-5-5"},
        {"at": iso(now + 300), "event": "routed", "episode": "e3", "model": "openai/gpt-5.5"})) + "\n")
    assert ch.hook_model(hook, now) == "anthropic/claude-opus-5-5"
    assert ch.hook_model(hook, now - 550) == "anthropic/claude-sonnet-5-5"
    assert ch.hook_model(hook, now - 450) is None          # e1 had ended: no episode open
    assert ch.hook_model(hook, now - 700) is None
    assert ch.action_model(H(), {"queued_at": now}, "r")[0] == "anthropic/claude-opus-5-5"   # ROOT/route-hook.jsonl

    # an OpenClaw session database (rows in ms), newest assistant message at or before the queue time
    db = d / "openclaw-agent.sqlite"
    con = sqlite3.connect(db)
    con.execute("create table transcript_events (created_at integer, event_json text, event_zstd blob)")
    for t, msg in ((now - 900, {"role": "assistant", "provider": "anthropic", "model": "claude-sonnet-5-5"}),
                   (now - 60, {"role": "assistant", "provider": "anthropic", "model": "claude-opus-5-5",
                               "response_model": "claude-opus-5-5-20261101"}),
                   (now - 30, {"role": "toolResult", "content": "ok"}),
                   (now + 120, {"role": "assistant", "provider": "openai", "model": "gpt-5.5"})):
        con.execute("insert into transcript_events values (?, ?, NULL)", (t * 1000, json.dumps({"message": msg})))
    con.commit()
    con.close()
    hook.unlink()
    assert ch.openclaw_model(str(db), now) == "claude-opus-5-5-20261101"
    assert ch.openclaw_model(str(db), now - 800) == "claude-sonnet-5-5"
    assert ch.openclaw_model(str(db), now - 5000) is None
    assert ch.openclaw_model(str(d / "missing.sqlite"), now) is None
    beacon = {"watch": [str(db), str(db) + "-wal"]}
    assert ch.action_model(H(beacon), {"queued_at": now}, "r")[0] == "claude-opus-5-5-20261101"
    assert ch.action_model(H(), {"queued_at": now}, "r")[0] is None   # nothing to read: no model

    # emit passes --model only to a CLI that has it
    class FakeHive(ch.Hive):
        def __init__(self, has):
            self.c, self.calls, self.has = {}, [], has

        def run(self, *cmd, cwd=None, check=True):
            if "--help" in cmd:
                return "usage: hive emit ... [--model MODEL]" if self.has else "usage: hive emit ..."
            self.calls.append(cmd)
            return json.dumps({"position": 1})
    for has in (True, False):
        f = FakeHive(has)
        f.emit("task.result_posted", "t1", None, [], model="claude-opus-5-5")
        assert ("--model" in f.calls[0]) == has, f.calls
print("PASSED: long-lived agents' questions, reports, results and reviews name the model that wrote them, or none")
