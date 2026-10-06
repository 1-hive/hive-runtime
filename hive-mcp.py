#!/usr/bin/env python3
"""hive MCP server: the chief of staff's tools over the record (1-hive PLAN, Phase E item 4).

A thin stdio MCP server over the `hive` and `hive-pin` CLIs, so it signs as whoever runs
it (HIVE_URL, HIVE_ID, HIVE_KEY_FILE, HIVEPIN_REPOSITORY_REGISTRY_PATH in its environment):
in 1-hive, `cos`, inside its own container. Reads: board, inbox, state, task, goal, events.
Writes: `emit`, which mints the pins it refers to from committed, pushed workspace or code
paths. Everything else (writing goal and order files, git) the chief of staff does with
its ordinary tools.

    hive-mcp.py      (stdio; configured in the chief of staff's --mcp-config)
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile

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


@mcp.tool()
def emit(event_type: str, data: dict, task_id: str = "", goal_id: str = "", refs: list[dict] | None = None) -> str:
    """Append an event, signed with this actor's key. `refs` are pins to mint first, each
    {"rel": "order", "repo": "workspace", "path": "projects/.../orders/x.md"} (a committed, pushed
    file or directory) or {"rel": "code", "repo": "mtg-player", "commit": "<sha>"} (a whole commit).
    The gateway checks legality and refuses with a code and a reason."""
    args = ["hive", "emit", event_type, "--data", json.dumps(data)]
    if task_id:
        args += ["--task", task_id]
    if goal_id:
        args += ["--goal", goal_id]
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
