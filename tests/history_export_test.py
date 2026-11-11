#!/usr/bin/env python3
"""history-export.py on a made-up hive: chats (a group upgraded to a supergroup), a workspace (an order, a result,
a review, an answer, an order's attachment), the record (hive.event/1), an OpenClaw agent (one event stored only
zstd-compressed, a private chat left out), a Claude Code session; formal / informal, task links, citations, redaction,
--merge, --since.

    python3 tests/history_export_test.py
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
T = "t-2026-11-01-widget"
SECRET = "ghp_" + "A" * 36


def run(*args, cwd=None):
    r = subprocess.run([sys.executable, str(HERE / "history-export.py"), *map(str, args)], capture_output=True, text=True, cwd=cwd)
    assert r.returncode == 0, r.stderr
    return r.stdout


def items(d: Path) -> dict:
    return {x["id"]: x for x in map(json.loads, (d / "items.jsonl").read_text().splitlines())}


def main():
    tmp = Path(tempfile.mkdtemp())
    # chats: old group id -5, new -100, same name
    dlg = tmp / "dialogue.jsonl"
    dlg.write_text("".join(json.dumps(e) + "\n" for e in [
        {"chat": -5, "chat_name": "work", "at": "2026-10-30T10:00:00+00:00", "from": "andy", "from_id": 8, "id": 7, "text": "old times"},
        {"chat": -100, "chat_name": "work", "at": "2026-11-01T09:00:00+00:00", "from": "ben", "from_id": 9, "id": 7,
         "text": "please build the widget"},
        {"chat": -100, "chat_name": "work", "at": "2026-11-01T09:01:00+00:00", "from": "protohive_cos_bot", "id": 8,
         "sent": True, "bot": True, "reply_to": 7, "text": f"Created {T} for ProtoBot."},
        {"chat": -100, "chat_name": "work", "at": "2026-11-01T09:02:00+00:00", "from": "protobot", "from_id": 50, "bot": True,
         "id": 9, "text": "on it"},
    ]))
    groups = tmp / "groups.json"
    groups.write_text(json.dumps({"chats": [{"name": "work", "chat_id": -100}],
                                  "humans": [{"name": "ben", "user_id": 9}, {"name": "andy", "user_id": 8}],
                                  "bots": [{"name": "protobot", "user_id": 50, "actor": "worker.protobot"}]}))
    # workspace
    ws = tmp / "ws"
    p = ws / "projects" / "widgets"
    for d in ("orders/2026-11-01-widget-build", "reports", "questions"):
        (p / d).mkdir(parents=True)
    (p / "orders" / "2026-11-01-widget-build.md").write_text(
        "# Order: Build the widget\n\nAsked by Ben, work group, telegram:-100/7.\n")
    (p / "orders" / "2026-11-01-widget-build" / "spec.md").write_text("# Spec\n")
    (p / "reports" / f"2026-11-01-{T}-result-1.md").write_text(f"# {T}: result 1\n\nworker.protobot, 2026-11-01\n\nDone.\n")
    (p / "reports" / f"2026-11-02-{T}-review-1.md").write_text(f"# {T}: review 1\n\nreviewer.claude, 2026-11-02\n\nPassed.\n")
    (p / "questions" / "2026-11-01-widget-answer-1.md").write_text("# Answer\n\nUse blue.\n")
    (ws / "README.md").write_text("# Workspace\n")
    g = ["git", "-C", str(ws), "-c", "user.name=cos", "-c", "user.email=cos@x"]
    subprocess.run(["git", "init", "-q", str(ws)], check=True)
    subprocess.run([*g, "add", "-A"], check=True)
    subprocess.run([*g, "commit", "-q", "-m", "all", "--date", "2026-11-01T09:05:00Z"], check=True,
                   env={"GIT_COMMITTER_DATE": "2026-11-01T09:05:00Z", "PATH": "/usr/bin:/bin"})
    # record
    rec = tmp / "record.jsonl"
    ev = lambda pos, typ, actor, cls, **kw: {"position": pos, "event_id": f"e{pos}", "type": typ, "schema": "hive.event/1",
                                             "actor": {"id": actor, "class": cls}, "recorded_at": f"2026-11-01T09:{pos:02d}:00Z",
                                             "refs": [], "data": {}, **kw}
    rec.write_text("".join(json.dumps(e) + "\n" for e in [
        ev(1, "actor.declared", "worker.protobot", "worker"),
        ev(2, "task.created", "cos", "chief_of_staff", task=T, cause={"external": "telegram:-100/7"},
           data={"title": "Build the widget", "project": "widgets"},
           refs=[{"rel": "order", "pin": {"repository": "workspace", "path": "projects/widgets/orders/2026-11-01-widget-build.md"}}]),
        ev(3, "task.assigned", "cos", "chief_of_staff", task=T, cause={"event": "e2"}, data={"to": "worker.protobot"}),
        ev(4, "review.recorded", "reviewer.claude", "reviewer", task=T, data={"verdict": "passed"},
           refs=[{"rel": "review", "pin": {"repository": "workspace", "path": f"projects/widgets/reports/2026-11-02-{T}-review-1.md"}}]),
        ev(5, "task.closed", "cos", "chief_of_staff", task=T),
    ]))
    # an OpenClaw agent: a group session (one event only zstd-compressed) and a private chat
    db = tmp / "openclaw-agent.sqlite"
    c = sqlite3.connect(db)
    c.executescript("create table transcript_events(session_id, seq, event_json, created_at, event_zstd, event_utf8_bytes, navigation_json);"
                    "create table session_windows(session_id, session_key);"
                    "create table session_nodes(session_key, current_session_id, entry_json);")
    c.execute("insert into session_windows values ('g1', 'agent:main:telegram:group:-100'), ('d1', 'agent:main:telegram:direct:9')")
    msg = lambda role, content: json.dumps({"type": "message", "timestamp": "2026-11-01T09:03:00Z",
                                            "message": {"role": role, "content": content}})
    z = subprocess.run(["zstd", "-q", "-c"], input=msg("assistant", [{"type": "text", "text": f"Working on {T}"}]).encode(),
                       capture_output=True, check=True).stdout
    c.executemany("insert into transcript_events values (?,?,?,?,?,?,?)", [
        ("g1", 1, msg("user", f"[Chat] ben: please build the widget ({T})"), 0, None, 0, None),
        ("g1", 2, msg("assistant", [{"type": "thinking", "thinking": "plan it"},
                                    {"type": "toolCall", "name": "exec", "arguments": {"cmd": f"echo {SECRET}"}}]), 0, None, 0, None),
        ("g1", 3, msg("toolResult", [{"type": "text", "text": "raw output"}]), 0, None, 0, None),
        ("g1", 4, "", 0, z, 0, None),
        ("d1", 1, msg("user", "a private word"), 0, None, 0, None),
    ])
    c.commit()
    c.close()
    # a Claude Code session: a group envelope turn and a private turn
    cl = tmp / "claude"
    cl.mkdir()
    (cl / "abcdef12.jsonl").write_text("".join(json.dumps(x) + "\n" for x in [
        {"type": "user", "timestamp": "2026-11-01T09:04:00Z", "message": {"content": "A message from a group chat ..."}},
        {"type": "assistant", "timestamp": "2026-11-01T09:04:05Z", "message": {"content": [{"type": "text", "text": "Noted."}]}},
        {"type": "user", "timestamp": "2026-11-01T09:06:00Z", "message": {"content": "hey, just between us"}},
        {"type": "assistant", "timestamp": "2026-11-01T09:06:05Z", "message": {"content": [{"type": "text", "text": "Sure."}]}},
    ]))

    # sessions alone (the root job), then everything with --merge (the chief of staff)
    run("--out", tmp / "s", "--groups", groups, "--openclaw", f"worker.protobot={db}")
    out = run("--out", tmp / "all", "--dialogue", dlg, "--workspace", ws, "--record", rec, "--groups", groups,
              "--claude", f"cos={cl}", "--merge", tmp / "s" / "items.jsonl", "--zip", "--missing", "pop-os")
    assert "tasks" in out and (tmp / "all.zip").exists(), out
    I = items(tmp / "all")

    # chats: both chat ids, actors by kind
    assert I["chat:-5:7"]["text"] == "old times" and I["chat:-100:7"]["actor"] == "ben"
    assert I["chat:-100:8"]["actor_type"] == "cos" and I["chat:-100:8"]["reply_to"] == "chat:-100:7"
    assert I["chat:-100:9"]["actor"] == "worker.protobot" and I["chat:-100:8"]["refs"] == [T]
    # workspace: kinds, authors from the report's second line, task links, the order's citation, formal
    o = I["ws:projects/widgets/orders/2026-11-01-widget-build.md"]
    assert o["kind"] == "order" and o["task"] == T and o["title"] == "Build the widget" and "chat:-100:7" in o["refs"], o
    assert I["ws:projects/widgets/orders/2026-11-01-widget-build/spec.md"]["kind"] == "attachment"
    r = I[f"ws:projects/widgets/reports/2026-11-02-{T}-review-1.md"]
    assert r["kind"] == "review" and r["actor"] == "reviewer.claude" and r["formal"] and r["task"] == T, r
    a = I["ws:projects/widgets/questions/2026-11-01-widget-answer-1.md"]
    assert a["kind"] == "answer" and a["task"] == T, a
    # record: no liveness events; cause as reply_to; pins as refs
    assert "rec:1" not in I and I["rec:2"]["reply_to"] == "chat:-100:7" and I["rec:3"]["reply_to"] == "rec:2"
    assert I["rec:4"]["title"].endswith("passed") and I["rec:4"]["refs"] == [f"ws:projects/widgets/reports/2026-11-02-{T}-review-1.md"]
    # sessions: the zstd event, thinking, a redacted tool call, no tool output, no private chat; tied to the task
    S = [x for x in I.values() if x["source"] == "session"]
    texts = " | ".join(x["text"] for x in S)
    assert f"Working on {T}" in texts and "plan it" in texts and "raw output" not in texts, texts
    assert SECRET not in texts and "[redacted]" in texts, texts
    assert "a private word" not in texts and "just between us" not in texts and "Sure." not in texts and "Noted." in texts
    assert {x["kind"] for x in S} >= {"prompt", "thinking", "tool_call", "message"}
    assert all(x["task"] == T for x in S if "worker.protobot" in x["id"]), [x for x in S if not x["task"]]
    assert not any(x["formal"] for x in S) and all(x["formal"] for x in I.values() if x["source"] in ("workspace", "record"))
    # tasks.jsonl
    t = json.loads((tmp / "all" / "tasks.jsonl").read_text().splitlines()[0])
    assert t["title"] == "Build the widget" and t["assignees"] == ["worker.protobot"] and t["verdicts"] == ["passed"]
    assert t["asked_in"] == "telegram:-100/7" and t["closed"] and len(t["items"]) >= 8, t
    assert "pop-os" in (tmp / "all" / "README.md").read_text()
    # --since
    run("--out", tmp / "recent", "--dialogue", dlg, "--since", "2026-11-01")
    assert "chat:-5:7" not in items(tmp / "recent")
    shutil.rmtree(tmp)
    print("ok")


if __name__ == "__main__":
    main()
