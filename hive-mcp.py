#!/usr/bin/env python3
"""hive MCP server: the chief of staff's tools over the record (1-hive PLAN, Phase E item 4).

A thin stdio MCP server over the `hive` and `hive-pin` CLIs, so it signs as whoever runs
it (HIVE_URL, HIVE_ID, HIVE_KEY_FILE, HIVEPIN_REPOSITORY_REGISTRY_PATH in its environment):
in 1-hive, `cos`, inside its own container. Reads: board, inbox, state, task, goal, events,
and `floor`: what each agent's process is doing now, from the Floor (hive-bridge floor.py, at
HIVE_FLOOR_URL, default http://127.0.0.1:8476, open from loopback), which holds the agents' beacons:
not the record, so not authoritative, but the only place a stopped or failing agent shows. Writes: `emit`, which mints the pins it refers to from committed, pushed workspace or code
paths. Everything else (writing goal and order files, git) the chief of staff does with
its ordinary tools.

    hive-mcp.py      (stdio; configured in the chief of staff's --mcp-config)
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import urllib.request

try:   # mcp 2.x
    from mcp.server.mcpserver import MCPServer
except ImportError:   # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer

mcp = MCPServer("hive")


def run(*args: str) -> str:
    r = subprocess.run(list(args), capture_output=True, text=True, env=os.environ)
    out = (r.stdout or "").strip()
    if r.returncode != 0:
        return f"refused or failed (exit {r.returncode}): {(r.stderr or out).strip()[-2000:]}"
    return out


@mcp.tool()
def board() -> str:
    """The hive's board: goals and their open tasks."""
    return run("hive", "board")


@mcp.tool()
def inbox(for_class: str = "chief_of_staff") -> str:
    """What needs attention, by the profile's fixed inbox rules (chief_of_staff or operator)."""
    return run("hive", "inbox", "--for", for_class, "--json")


@mcp.tool()
def state() -> str:
    """The whole folded state (goals, tasks, actors). Large: prefer board, task or goal."""
    return run("hive", "state")


@mcp.tool()
def task(task_id: str) -> str:
    """A task's state and its full history."""
    return run("hive", "task", task_id)


@mcp.tool()
def goal(goal_id: str) -> str:
    """A goal's state, its tasks and its history."""
    return run("hive", "goal", goal_id)


@mcp.tool()
def events(after: int = 0, event_type: str = "", task_id: str = "", goal_id: str = "", limit: int = 100) -> str:
    """Events after a position, optionally of one type, task or goal; at most `limit` (the last ones)."""
    args = ["hive", "events", "--after", str(after)]
    for flag, v in (("--type", event_type), ("--task", task_id), ("--goal", goal_id)):
        if v:
            args += [flag, v]
    lines = run(*args).splitlines()
    return "\n".join(lines[-max(1, min(limit, 1000)):])


FLOOR_URL = os.environ.get("HIVE_FLOOR_URL", "http://127.0.0.1:8476").rstrip("/")


def floor_get(path: str) -> dict:
    with urllib.request.urlopen(FLOOR_URL + path, timeout=20) as r:
        return json.load(r)


@mcp.tool()
def floor(actor: str = "") -> str:
    """What each agent's process is doing now (the Floor, from the agents' beacons; the record can't show it):
    whether it is working, idle, offline or can't run and why (e.g. a reviewer out of credits), and what it
    owes. Also what the Floor puts before the operators ("needs"). With `actor` (e.g. "reviewer.astra"), that
    agent only, with its recent events. Not the record: report it as what the agent's runner says, and act
    through the record as usual."""
    try:
        if actor:
            b = floor_get("/api/bot/" + actor)
            keep = ("id", "name", "class", "machine", "model", "now", "plate", "queued", "beacon_age", "note", "events")
            out = {k: b[k] for k in keep if k in b}
            out["events"] = (out.get("events") or [])[-15:]
            return json.dumps(out, ensure_ascii=False, default=str)
        bots = floor_get("/api/bots")["bots"]
        needs = floor_get("/api/needs")["needs"]
        keep = ("id", "name", "class", "now", "plate", "beacon_age")
        return json.dumps({"bots": [{k: b[k] for k in keep if k in b} for b in bots], "needs": needs},
                          ensure_ascii=False, default=str)
    except Exception as e:
        return f"the Floor at {FLOOR_URL} didn't answer: {type(e).__name__}: {e}"


@mcp.tool()
def emit(event_type: str, data: dict, task_id: str = "", goal_id: str = "", refs: list[dict] | None = None,
         cause_event: str = "", cause_external: str = "") -> str:
    """Append an event, signed with this actor's key. `refs` are pins to mint first, each
    {"rel": "order", "repo": "workspace", "path": "projects/.../orders/x.md"} (a committed, pushed
    file or directory) or {"rel": "code", "repo": "mtg-player", "commit": "<sha>"} (a whole commit).
    `cause_event` is the id of the event this one responds to, `cause_external` what outside the record
    caused it (e.g. "telegram:<chat id>/<message id>"); record A5, hive-record 1.3.0 and later.
    The gateway checks legality and refuses with a code and a reason."""
    args = ["hive", "emit", event_type, "--data", json.dumps(data)]
    if task_id:
        args += ["--task", task_id]
    if goal_id:
        args += ["--goal", goal_id]
    if cause_event:
        args += ["--cause-event", cause_event]
    if cause_external:
        args += ["--cause-external", cause_external]
    with tempfile.TemporaryDirectory() as tmp:
        for i, r in enumerate(refs or []):
            pin = os.path.join(tmp, f"{i}.pin")
            mint = ["hive-pin", "mint", r["repo"]] + ([r["path"]] if r.get("path") else []) \
                + (["--commit", r["commit"]] if r.get("commit") else []) + ["--output", pin]
            out = run(*mint)
            if out.startswith("refused or failed"):
                return f"pin for {r}: {out}"
            args += ["--ref", f"{r['rel']}={pin}"]
        return run(*args)


if __name__ == "__main__":
    mcp.run()
