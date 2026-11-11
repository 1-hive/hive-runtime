#!/usr/bin/env python3
"""A hive's history as one dataset, for tools that navigate and visualize it (e.g. Scout): the informal side (group
chats, agents' own sessions) and the formal side (the workspace's orders, results, reviews, questions; the record's
events), one item per line in items.jsonl, every item with the same fields, so a tool can filter formal / informal,
by task, by actor, by time.

    history-export.py --out DIR [--dialogue <bridge .telegram/dialogue.jsonl>] [--workspace <workspace clone>]
                      [--record <`hive export` JSONL> | --record-live] [--groups <telegram-groups.json>]
                      [--openclaw <agent>=<agent sqlite> ...] [--claude <agent>=<.claude-state/projects/<dir>> ...]
                      [--iter-channel <agent>=<iter-channel dir> ...] [--merge <items.jsonl> ...]
                      [--include-private] [--since YYYY-MM-DD] [--zip]

Each source is optional. --record-live runs `hive export` with the caller's HIVE_* environment (the chief of staff's
own key). The agents' sessions (--openclaw, --claude, --iter-channel) are often readable only by root: a root job
exports them alone (deploy: history-collect), and the chief of staff's export takes that file in with --merge.
Private one-to-one chats (a bot with its owner, an operator with the chief of staff, its console) are left out unless
--include-private. Session items are an agent's messages, its thinking and its tool calls (name and the start of the
arguments); tool output is left out, and text that looks like a key or token is redacted everywhere. DIR gets items.jsonl, tasks.jsonl (one line per task: title, project, who, state, times, its items) and
README.md (the fields, the counts). --zip also writes DIR.zip.

Item fields (README.md says the same for the reader):
  id          unique: chat:<chat id>:<message id>, ws:<path>, rec:<position>, session:<agent>:<session>:<n>
  source      chat | workspace | record | session
  formal      true for workspace and record items (the hive's formal process), false for chats and sessions
  kind        chat: message; session: message, prompt (what the agent was given), thinking, tool_call; workspace: order, result, review, question, answer, checkpoint, blocked, report,
              progress, attachment, request, proposal, doc; record: the event type (task.created, review.recorded, ...)
  at          ISO 8601 UTC
  actor       who: a human's name (andy, ben, ...), an agent's actor id (worker.protocosmo2, reviewer.claude), cos
  actor_type  human | agent | cos | system
  channel     where: the chat's name, the project, the agent's session
  task        the task id it belongs to, if any
  project     the workspace project, if any
  title       a short title (first heading, event summary)
  text        the full text (a message, a document, an event's payload as JSON)
  reply_to    the id of the item it answers, if known
  refs        ids of items and tasks it mentions (task ids, telegram:<chat>/<msg> citations as chat ids)
  location    where it lives: a path in the workspace, a Telegram chat/message
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

TASK_RE = re.compile(r"\bt-\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9-]*[a-z0-9]")
TG_RE = re.compile(r"telegram:(-?\d+)/(\d+)")
REPORT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(t-\d{4}-\d{2}-\d{2}-[a-z0-9-]+?)-"
                       r"(result|review|question|answer|checkpoint|blocked|progress)-(\d+)\.md$")
ANSWER_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(.+?)-answer-(\d+)\.md$")   # questions/<date>-<task slug>-answer-<n>.md
REPORT_KINDS = ("result", "review", "question", "answer", "checkpoint", "blocked", "progress")
TEXT_SUFFIXES = {".md", ".txt", ".json", ".jsonl", ".yaml", ".yml", ".csv", ".tsv", ".py", ".metta", ".toml", ".log", ""}
TEXT_MAX = 1_000_000
UTC = dt.timezone.utc


def iso(t) -> str:
    """ISO 8601 UTC from a datetime, an epoch number or an ISO string."""
    if isinstance(t, (int, float)):
        return dt.datetime.fromtimestamp(t, UTC).isoformat()
    if isinstance(t, str):
        try:
            d = dt.datetime.fromisoformat(t.replace("Z", "+00:00"))
            return (d if d.tzinfo else d.replace(tzinfo=UTC)).astimezone(UTC).isoformat()
        except ValueError:
            return t
    return t.astimezone(UTC).isoformat()


def refs_in(text: str, chat_ids: dict) -> list[str]:
    out = []
    for t in TASK_RE.findall(text or ""):
        if t not in out:
            out.append(t)
    for chat, msg in TG_RE.findall(text or ""):
        cid = chat_ids.get(int(chat), int(chat))
        r = f"chat:{cid}:{msg}"
        if r not in out:
            out.append(r)
    return out


def item(**kw) -> dict:
    base = {"id": None, "source": None, "formal": False, "kind": None, "at": None, "actor": None, "actor_type": None,
            "channel": None, "task": None, "project": None, "title": None, "text": None, "reply_to": None,
            "refs": [], "location": None}
    base.update(kw)
    return base


class Actors:
    """Names as people know them: a chat's sender name to the agent's actor id where telegram-groups.json says so."""

    def __init__(self, groups: dict):
        self.humans = {h["name"] for h in groups.get("humans", [])}
        self.bots = {b["name"]: b.get("actor") or b["name"] for b in groups.get("bots", [])}
        self.bots.update({(b.get("username") or "").lower(): b.get("actor") or b["name"]
                          for b in groups.get("bots", []) if b.get("username")})

    def of_chat(self, e: dict) -> tuple[str, str]:
        if e.get("sent"):
            return "cos", "cos"
        name = e.get("from") or "someone"
        if name in self.bots or name.lower() in self.bots:
            return self.bots.get(name) or self.bots[name.lower()], "agent"
        if e.get("bot"):
            return name, "agent"
        return name, "human"

    @staticmethod
    def of_id(a: str | None) -> tuple[str | None, str]:
        if not a:
            return None, "system"
        if a == "cos" or a.startswith("cos"):
            return "cos", "cos"
        if a.split(".")[0] in ("worker", "reviewer", "agent"):
            return a, "agent"
        if a in ("operator", "telegram") or a.startswith("operator"):
            return a, "human" if a == "operator" else "system"
        return a, "system"


# --- chats -----------------------------------------------------------------------------------------------------

def chats(path: Path, actors: Actors, since: str) -> tuple[list[dict], dict]:
    """The bridge's dialogue log: group chats only. A group upgraded to a supergroup keeps its name (channel) and its
    messages keep the chat id of their time, as citations (telegram:<chat>/<message>) do."""
    lines = []
    with path.open() as f:
        for line in f:
            try:
                lines.append(json.loads(line))
            except ValueError:
                pass
    chat_ids = {e["chat"]: e["chat"] for e in lines}
    out, n = [], 0
    for e in lines:
        if e.get("at", "") < since:
            continue
        text = e.get("text") or ""
        files = [d.get("name") for d in e.get("files") or [] if d.get("name")]
        if not text.strip() and not files:
            continue
        actor, kind = actors.of_chat(e)
        chat = e["chat"]
        if e.get("id") is not None:
            iid = f"chat:{chat}:{e['id']}"
        else:
            n += 1
            iid = f"chat:{chat}:x{n}"
        out.append(item(
            id=iid, source="chat", formal=False, kind="message", at=iso(e["at"]), actor=actor, actor_type=kind,
            channel=e.get("chat_name"), title=None,
            text=text + "".join(f"\n[attached file: {x}]" for x in files),
            reply_to=f"chat:{chat}:{e['reply_to']}" if e.get("reply_to") is not None else None,
            refs=refs_in(text, chat_ids), location=f"telegram:{chat}/{e['id']}" if e.get("id") is not None else f"telegram:{chat}"))
    return out, chat_ids


# --- workspace -------------------------------------------------------------------------------------------------

def git(ws: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "safe.directory=*", "-C", str(ws), *args], capture_output=True, text=True,
                          check=True).stdout


def workspace(ws: Path, chat_ids: dict, since: str) -> list[dict]:
    """Every file in the workspace's main history: its kind from where it is and its name, its time and author from
    the commit that added it."""
    added: dict[str, tuple[str, str]] = {}
    cur = None
    for line in git(ws, "log", "--reverse", "-M", "--diff-filter=AR", "--name-only", "--format=@@%aI|%an").splitlines():
        if line.startswith("@@"):
            at, _, who = line[2:].partition("|")
            cur = (iso(at), who)
        elif line.strip() and cur and line not in added:
            added[line] = cur
    files = git(ws, "ls-files").splitlines()
    known = {m.group(2) for m in (REPORT_RE.match(r.rsplit("/", 1)[-1]) for r in files) if m}
    out = []
    for rel in files:
        at, who = added.get(rel, (None, None))
        if at and at < since:
            continue
        p = ws / rel
        parts = rel.split("/")
        project = parts[1] if parts[0] == "projects" and len(parts) > 2 else None
        folder = parts[2] if project and len(parts) > 3 else parts[0]
        kind, task = "doc", None
        m = REPORT_RE.match(parts[-1])
        a = ANSWER_RE.match(parts[-1]) if folder == "questions" else None
        if m:
            task, kind = m.group(2), m.group(3)
        elif a:
            kind = "answer"
            slug = a.group(2)
            task = slug if slug.startswith("t-") else next(
                (c for c in (f"t-{a.group(1)}-{slug}", f"t-{slug}") if c in known), None)
        elif folder == "orders" and len(parts) > 4:   # a file attached to an order: orders/<order>/<file>
            kind, stem = "attachment", parts[3]
            task = f"t-{stem}" if f"t-{stem}" in known else None
        elif folder == "orders":
            kind = "order"
            task = f"t-{Path(rel).stem}" if f"t-{Path(rel).stem}" in known else None
        elif parts[0] == "hive-requests":
            kind = "request"
        elif parts[0] == "proposals":
            kind = "proposal"
        text, title = None, None
        if p.suffix.lower() in TEXT_SUFFIXES and p.is_file() and p.stat().st_size <= TEXT_MAX:
            text = p.read_bytes().decode("utf-8", errors="replace")
            title = next((x.lstrip("# ").strip() for x in text.splitlines() if x.startswith("#")), None)
            # a report's second line names its author: "reviewer.claude, 2026-11-06"
            head = [x for x in text.splitlines()[:4] if x.strip()]
            if m and len(head) > 1 and re.match(r"^[a-z][\w.-]+, \d{4}-\d{2}-\d{2}", head[1]):
                who = head[1].split(",")[0]
        elif p.is_file():
            text = f"[{p.suffix.lstrip('.') or 'binary'} file, {p.stat().st_size} bytes, not included]"
        if kind == "order" and text:
            title = re.sub(r"^Order:\s*", "", title or "") or None
        if task is None:
            named = [t for t in TASK_RE.findall(parts[-1]) if t in known] or \
                    [k for k in known if k in parts[-1]]
            task = max(named, key=len) if named else None
        if task is None and kind in ("order", "answer", "attachment") and text:
            mentioned = [t for t in TASK_RE.findall(text) if t in known]
            task = mentioned[0] if mentioned else None
        actor, atype = Actors.of_id(who)
        out.append(item(
            id=f"ws:{rel}", source="workspace", formal=True, kind=kind, at=at, actor=actor, actor_type=atype,
            channel=project or parts[0], task=task, project=project, title=title or parts[-1], text=text,
            refs=[r for r in refs_in(text or "", chat_ids) if r != task], location=rel))
    return out


# --- record ----------------------------------------------------------------------------------------------------

def record_lines(path: Path | None) -> list[dict]:
    if path is None:
        r = subprocess.run(["hive", "export"], capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"hive export: {r.stderr.strip() or r.stdout.strip()}")
        raw = r.stdout.splitlines()
    else:
        raw = path.read_text().splitlines()
    out = []
    for line in raw:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


RECORD_SKIP = ("actor.declared", "gateway.started", "gateway.rejected")   # liveness and plumbing, not the work
CLASS_TYPE = {"chief_of_staff": "cos", "worker": "agent", "reviewer": "agent", "operator": "human"}


def event_summary(typ: str, data: dict) -> str:
    """A few words on what an event says, for its title."""
    if typ == "task.created":
        return data.get("title") or ""
    if typ in ("task.assigned", "review.assigned", "task.reassigned"):
        return f"to {data.get('to')}" if data.get("to") else ""
    if typ == "review.recorded":
        return data.get("verdict") or ""
    for k in ("reason", "question", "summary", "note", "answer"):
        if isinstance(data.get(k), str):
            return data[k][:120]
    return ""


def record(events: list[dict], chat_ids: dict, since: str, skip=RECORD_SKIP) -> tuple[list[dict], dict, dict]:
    """The record's log (hive export, hive.event/1): an item per event, a summary per task, and the workspace paths
    its events pin (path -> task), so workspace items get their task even when a file's name doesn't say it."""
    pos = {e.get("event_id"): e.get("position") for e in events}
    out, tasks, pinned = [], {}, {}
    for e in events:
        typ, task, data = e.get("type") or "", e.get("task"), e.get("data") or {}
        at = iso(e["recorded_at"]) if e.get("recorded_at") else None
        actor = (e.get("actor") or {}).get("id")
        atype = CLASS_TYPE.get((e.get("actor") or {}).get("class"), "system")
        refs = []
        for r in e.get("refs") or []:
            pin = r.get("pin") or {}
            if pin.get("repository") == "workspace" and pin.get("path"):
                refs.append(f"ws:{pin['path']}")
                if task:
                    pinned.setdefault(pin["path"], task)
            elif pin.get("repository"):
                refs.append(f"{pin['repository']}@{(pin.get('commit_oid') or '').split(':')[-1][:12]}")
        cause = e.get("cause") or {}
        reply_to = None
        if cause.get("external"):
            m = TG_RE.search(cause["external"])
            reply_to = f"chat:{m.group(1)}:{m.group(2)}" if m else None
        elif cause.get("event") in pos:
            reply_to = f"rec:{pos[cause['event']]}"
        if task:
            t = tasks.setdefault(task, {"task": task, "title": None, "project": None, "created": at, "closed": None,
                                        "state": None, "assignees": [], "reviewers": [], "verdicts": [],
                                        "asked_in": None, "items": []})
            if typ == "task.created":
                t.update(title=data.get("title"), project=data.get("project"), created=at,
                         asked_in=cause.get("external"))
            if typ in ("task.assigned", "task.reassigned") and data.get("to") and data["to"] not in t["assignees"]:
                t["assignees"].append(data["to"])
            if typ == "review.recorded":
                if actor not in t["reviewers"]:
                    t["reviewers"].append(actor)
                t["verdicts"].append(data.get("verdict"))
            if typ.startswith("task."):
                t["state"] = typ.split(".", 1)[1]
            if typ in ("task.closed", "task.cancelled"):
                t["closed"] = at
        if typ in skip or (at and at < since):
            continue
        project = (tasks.get(task) or {}).get("project") or data.get("project")
        text = json.dumps({k: v for k, v in data.items() if k != "ext"} | ({"ext": data["ext"]} if "ext" in data else {}),
                          ensure_ascii=False, sort_keys=True)
        title = " ".join(x for x in (typ, task or "", event_summary(typ, data)) if x)
        out.append(item(
            id=f"rec:{e.get('position')}", source="record", formal=True, kind=typ, at=at, actor=actor, actor_type=atype,
            channel=project or "record", task=task, project=project, title=title, text=text, reply_to=reply_to,
            refs=refs + [r for r in refs_in(text, chat_ids) if r != task and r not in refs],
            location=f"record position {e.get('position')}"))
    return out, tasks, pinned


# --- agents' sessions ------------------------------------------------------------------------------------------

SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_-]{16,}|sk-ant-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
                       r"|\b\d{8,10}:AA[A-Za-z0-9_-]{30,}|xox[abpr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}"
                       r"|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
                       r"|(?i:(?:authorization|bearer|api[_-]?key|token|secret|password)[\"']?\s*[:=]\s*[\"']?)[A-Za-z0-9._~+/=-]{16,})")
PROMPT_MAX, TEXT_CAP, ARGS_MAX = 4000, 50_000, 300


def redact(text):
    if not isinstance(text, str):
        return text
    return SECRET_RE.sub(lambda m: m.group(0)[:4] + "…[redacted]", text)


def cap(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + f"\n[… {len(text) - n} more characters]"


def blocks_to_items(agent: str, sid: str, channel: str, role: str, content, at, n: int, location: str,
                    prompt_max: int = PROMPT_MAX) -> list[dict]:
    """One turn's content as items: a user turn is a prompt (or a person's message), an assistant turn gives a message
    per text block, a thinking item, and a tool_call per call. Tool results are left out."""
    out = []
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return out
    for b in content:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if role == "user":
            if t != "text" or not (b.get("text") or "").strip():
                continue
            kind, text, actor, atype = "prompt", cap(b["text"], prompt_max), None, "system"
        elif t == "text" and (b.get("text") or "").strip():
            kind, text, actor, atype = "message", cap(b["text"], TEXT_CAP), agent, "agent"
        elif t == "thinking" and (b.get("thinking") or "").strip():
            kind, text, actor, atype = "thinking", cap(b["thinking"], TEXT_CAP), agent, "agent"
        elif t in ("tool_use", "toolCall"):
            args = b.get("input") if "input" in b else b.get("arguments")
            kind, actor, atype = "tool_call", agent, "agent"
            text = f"{b.get('name')}: " + cap(json.dumps(args, ensure_ascii=False) if not isinstance(args, str) else args, ARGS_MAX)
        else:
            continue
        n += 1
        text = redact(text)
        out.append(item(id=f"session:{agent}:{sid}:{n}", source="session", formal=False, kind=kind,
                        at=iso(at) if at else None, actor=actor, actor_type=atype, channel=channel,
                        title=None, text=text, refs=refs_in(text, {}), location=location))
    return out


def openclaw_channel(key: str, groups: dict) -> tuple[str, bool]:
    """A session key's channel, and whether it's a private chat: agent:<a>:telegram:group:<id>, :telegram:direct:<id>,
    :cron:…, :openai:<uuid> (an Iter step), :hive:…"""
    parts = key.split(":")
    rest = parts[2:] if len(parts) > 2 else parts
    if rest[:2] == ["telegram", "group"] and len(rest) > 2:
        name = next((c["name"] for c in groups.get("chats", []) if str(c["chat_id"]) == rest[2]), rest[2])
        return f"telegram group {name}", False
    if rest[:2] == ["telegram", "direct"]:
        return "telegram private chat", True
    if rest and rest[0] == "openai":
        return "work step", False
    return " ".join(rest[:2]) or key, False


def zstd_text(blob: bytes) -> str:
    r = subprocess.run(["zstd", "-d", "-c", "-q"], input=blob, capture_output=True)
    return r.stdout.decode("utf-8", errors="replace") if r.returncode == 0 else ""


def openclaw(agent: str, db: Path, groups: dict, since: str, private: bool) -> list[dict]:
    """An OpenClaw agent's sessions (agents/<id>/agent/openclaw-agent.sqlite, read-only): transcript_events, with each
    session's channel from session_windows / session_nodes. Some events are stored only zstd-compressed."""
    import sqlite3
    c = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
    keys = {sid: key for sid, key in c.execute("select session_id, session_key from session_windows")}
    for key, cur, entry in c.execute("select session_key, current_session_id, entry_json from session_nodes"):
        for sid in [cur] + ((json.loads(entry or "{}").get("usageFamilySessionIds") or []) if entry else []):
            if sid:
                keys.setdefault(sid, key)
    out, counters = [], {}
    for sid, seq, ej, ez, created in c.execute(
            "select session_id, seq, event_json, event_zstd, created_at from transcript_events order by session_id, seq"):
        channel, is_private = openclaw_channel(keys.get(sid, ""), groups)
        if is_private and not private:
            continue
        raw = ej or (zstd_text(ez) if ez else "")
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        if ev.get("type") != "message":
            continue
        at = ev.get("timestamp") or (created / 1000 if created else None)
        if at and iso(at) < since:
            continue
        m = ev.get("message") or {}
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        n = counters.get(sid, 0)
        got = blocks_to_items(agent, sid[:8], f"{agent} {channel}", role, m.get("content"), at, n,
                              f"{db}#{sid}/{seq}", prompt_max=2000 if channel == "work step" else PROMPT_MAX)
        counters[sid] = n + len(got)
        out += got
    return out


def claude_sessions(agent: str, folder: Path, since: str, private: bool) -> list[dict]:
    """Claude Code sessions (a .claude-state/projects/<dir> of JSONL). A chat session's turns on a private message (not
    a group envelope or a hive notice) count as private."""
    out = []
    for f in sorted(folder.glob("*.jsonl")):
        sid, n, skip = f.stem[:8], 0, False
        for line in f.open(errors="replace"):
            try:
                x = json.loads(line)
            except ValueError:
                continue
            if x.get("type") not in ("user", "assistant"):
                continue
            m = x.get("message") or {}
            content = m.get("content")
            if x["type"] == "user" and isinstance(content, str):   # a new turn: is it private?
                skip = not private and not content.startswith(("A message from a group chat", "Task t-", "Proposal ",
                                                               "# Review", "A proposal", "Review "))
            if skip or (x.get("timestamp") and iso(x["timestamp"]) < since):
                continue
            got = blocks_to_items(agent, sid, f"{agent} session {sid}", x["type"], content, x.get("timestamp"), n,
                                  f"{f.name}#{x.get('uuid', '')}")
            n += len(got)
            out += got
    return out


def iter_channel(agent: str, folder: Path, since: str, private: bool) -> list[dict]:
    """An Iter bot's Telegram requests and replies (iter-channel/completed and outbox: one JSON file per request,
    times from the files). A private chat (positive chat id) is left out unless asked."""
    out, seen = [], set()
    for sub in ("outbox", "completed"):
        for f in sorted((folder / sub).glob("*.json")):
            try:
                x = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            src = x.get("source") or {}
            rid = x.get("request_id") or f.stem
            chat = src.get("chat_id")
            if rid in seen or (isinstance(chat, int) and chat > 0 and not private):
                continue
            seen.add(rid)
            at = iso(f.stat().st_mtime)
            if at < since:
                continue
            ch = f"{agent} telegram {chat}"
            if x.get("prompt"):
                out.append(item(id=f"session:{agent}:iter:{rid}:q", source="session", kind="message", at=at,
                                actor=(x.get("identity") or {}).get("name") if isinstance(x.get("identity"), dict) else None,
                                actor_type="human", channel=ch, text=redact(cap(x["prompt"], TEXT_CAP)),
                                location=f"telegram:{chat}/{src.get('message_id')}"))
            if x.get("response"):
                out.append(item(id=f"session:{agent}:iter:{rid}:a", source="session", kind="message", at=at,
                                actor=agent, actor_type="agent", channel=ch, text=redact(cap(x["response"], TEXT_CAP)),
                                reply_to=f"session:{agent}:iter:{rid}:q", location=str(f)))
    return out


# --- output ----------------------------------------------------------------------------------------------------

README = """# {hive}: history export

Made {made} by history-export.py (hive-runtime). One item per line in `items.jsonl`, oldest first; one line per task
in `tasks.jsonl`.

**Formal** items (`"formal": true`) are the hive's process: the workspace's orders, results, reviews, questions and
answers (`source: "workspace"`) and the record's events (`source: "record"`). **Informal** items are conversation: group
chats (`source: "chat"`) and the agents' own sessions (`source: "session"`).

## Counts

{counts}

## Item fields

| field | meaning |
|---|---|
| id | unique: `chat:<chat id>:<message id>`, `ws:<path>`, `rec:<position>`, `session:<agent>:<session>:<n>` |
| source | `chat`, `workspace`, `record`, `session` |
| formal | true for workspace and record items |
| kind | chats: `message`; sessions: `message`, `prompt` (what the agent was given), `thinking`, `tool_call`; workspace: `order`, `result`, `review`, `question`, `answer`, `checkpoint`, `progress`, `blocked`, `attachment` (a file given with an order), `request`, `proposal`, `doc`; record: the event type |
| at | ISO 8601, UTC |
| actor, actor_type | who (`andy`, `worker.protocosmo2`, `reviewer.claude`, `cos`) and `human`, `agent`, `cos` (the hive's chief of staff) or `system` |
| channel | the chat's name, the project, or the agent's session |
| task, project | the task id and workspace project it belongs to |
| title, text | a short title; the full text (a document's content, an event's payload as JSON) |
| reply_to | the id of the message it answers |
| refs | ids it mentions: task ids, and chat messages it cites (`chat:<chat id>:<message id>`) |
| location | a workspace path, `telegram:<chat>/<message>`, a record position, a session file |

Links between the two sides: an order cites the chat message that asked for the work (`refs`), every workspace and
record item carries its `task`, and chat messages mention task ids.

## Task fields (tasks.jsonl)

task, title, project, created, closed, state (the last task event), assignees, reviewers, verdicts (in order),
asked_in (the chat message the task came from, telegram:<chat>/<message>), items (ids of every item with this task).

Record events leave out actor.declared and gateway.* (liveness and plumbing). An event's `reply_to` is what caused it:
the chat message or the earlier event (`rec:<position>`); its `refs` are the workspace documents it pins (`ws:<path>`)
and code commits (`<repository>@<commit>`).

## Not included

{missing}
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--dialogue", type=Path)
    ap.add_argument("--workspace", type=Path)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--record", type=Path)
    g.add_argument("--record-live", action="store_true")
    ap.add_argument("--groups", type=Path)
    ap.add_argument("--openclaw", action="append", default=[], metavar="AGENT=SQLITE")
    ap.add_argument("--claude", action="append", default=[], metavar="AGENT=DIR")
    ap.add_argument("--iter-channel", action="append", default=[], metavar="AGENT=DIR")
    ap.add_argument("--merge", action="append", default=[], type=Path, metavar="ITEMS.jsonl")
    ap.add_argument("--include-private", action="store_true")
    ap.add_argument("--since", default="")
    ap.add_argument("--zip", action="store_true")
    ap.add_argument("--hive", default=os.environ.get("HIVE_ID") or "hive")
    ap.add_argument("--missing", action="append", default=[], help="a line for README's 'Not included'")
    a = ap.parse_args()

    groups = json.loads(a.groups.read_text()) if a.groups else {}
    actors = Actors(groups)
    items, chat_ids, tasks = [], {}, {}
    if a.dialogue:
        got, chat_ids = chats(a.dialogue, actors, a.since)
        items += got
    if a.workspace:
        items += workspace(a.workspace, chat_ids, a.since)
    if a.record or a.record_live:
        got, tasks, pinned = record(record_lines(a.record), chat_ids, a.since)
        items += got
        for it in items:   # a workspace file an event pins belongs to that event's task
            if it["source"] == "workspace" and not it["task"] and it["location"] in pinned:
                it["task"] = pinned[it["location"]]
    for spec in a.openclaw:
        agent, _, db = spec.partition("=")
        items += openclaw(agent, Path(db), groups, a.since, a.include_private)
    for spec in a.claude:
        agent, _, folder = spec.partition("=")
        items += claude_sessions(agent, Path(folder), a.since, a.include_private)
    for spec in a.iter_channel:
        agent, _, folder = spec.partition("=")
        items += iter_channel(agent, Path(folder), a.since, a.include_private)
    for f in a.merge:
        with f.open() as fh:
            items += [json.loads(x) for x in fh if x.strip()]
    for it in items:
        it["text"] = redact(it["text"])

    known = set(tasks) | {it["task"] for it in items if it["task"]}
    by_session: dict[str, set] = {}
    for it in items:   # a session that names exactly one known task in its prompts belongs to that task
        if it["source"] == "session" and it["kind"] == "prompt":
            by_session.setdefault(it["id"].rsplit(":", 1)[0], set()).update(r for r in it["refs"] if r in known)
    for it in items:
        if it["source"] == "session" and not it["task"]:
            named = by_session.get(it["id"].rsplit(":", 1)[0]) or set()
            if len(named) == 1:
                it["task"] = next(iter(named))
    for it in items:   # tie workspace items to their record task (orders are named by slug, not task id)
        if it["task"] and it["task"] not in tasks:
            tasks[it["task"]] = {"task": it["task"], "title": None, "project": it["project"], "created": None,
                                 "closed": None, "state": None, "assignees": [], "reviewers": [], "verdicts": [],
                                 "asked_in": None, "items": []}
        if it["task"]:
            tasks[it["task"]]["items"].append(it["id"])
            tasks[it["task"]]["project"] = tasks[it["task"]]["project"] or it["project"]
    items.sort(key=lambda x: (x["at"] or "", x["id"]))

    a.out.mkdir(parents=True, exist_ok=True)
    with (a.out / "items.jsonl").open("w") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    with (a.out / "tasks.jsonl").open("w") as f:
        for t in sorted(tasks.values(), key=lambda t: (t["created"] or "", t["task"])):
            f.write(json.dumps(t, ensure_ascii=False) + "\n")
    counts: dict[tuple, int] = {}
    for it in items:
        k = (it["source"], it["kind"] if it["source"] != "record" else "events")
        counts[k] = counts.get(k, 0) + 1
    span = [x["at"] for x in items if x["at"]]
    lines = [f"{len(items)} items" + (f", {min(span)[:10]} to {max(span)[:10]}" if span else "") + f"; {len(tasks)} tasks.", ""]
    lines += [f"- {s} / {k}: {n}" for (s, k), n in sorted(counts.items())]
    missing = a.missing or ["(nothing noted)"]
    (a.out / "README.md").write_text(README.format(hive=a.hive, made=dt.datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
                                                    counts="\n".join(lines), missing="\n".join(f"- {m}" for m in missing)))
    print(f"{len(items)} items, {len(tasks)} tasks -> {a.out}")
    if a.zip:
        z = a.out.with_suffix(".zip")
        with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in ("README.md", "items.jsonl", "tasks.jsonl"):
                zf.write(a.out / name, f"{a.out.name}/{name}")
        print(f"zip: {z} ({z.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
