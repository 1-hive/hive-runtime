#!/usr/bin/env python3
"""Does this Iter agent work with the hive adapter? Run it against an agent's folder, of any Iter variant.

    conformance.py --folder <the agent's Iter folder> [--python <the agent's interpreter>] [--prompt FILE] [--json]

Iter agents differ and modify themselves; the hive adapter (channels/hive.py, tools/hive_task.py) must
keep working under each of them. This checks the contract between the two, the way the agent itself
calls the adapter: through its own `iter.py --invoke` worker (a fresh process per call), under its own
timeouts and tool limits. It never touches the agent's live queue: the calls run in a scratch folder
holding a copy of the agent's iter.py, its send tool and its installed adapter, in sidecar mode (so no
daemon starts and nothing reaches the record).

Checks (FAIL breaks hive work; WARN works but with a limit worth knowing; exit 1 on any FAIL):
  installed       channels/hive.py, tools/hive_task.py and .hive/config.json are in the folder
  invoke          iter.py has the --invoke worker the adapter is called through
  tool_slot       hive_task is among the tools Iter loads (it loads the first MAX_TOOLS, sorted)
  adapter_copy    the installed files match this adapter (WARN: reinstall with install-agent.py)
  config          sidecar mode, or a spawned daemon whose key only the agent's user can read
  prompt_rule     the hive rule is in the agent's prompt (install-agent.py's block, or by hand)
  tool_metadata   Iter reads hive_task's parameters and description as the adapter defines them
  receive         a queued message comes out of receive() once, within Iter's limit for channels
  probe           Iter's channel probe (__has_attrs__, request binding) answers for the hive channel
  hive_task       the tool queues an action within Iter's limit for tools
  send            the agent's own send tool reaches the hive channel, unbound and bound to a chat request

Tested by tests/iter_conformance_test.py.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BEGIN = "# --- hive (hive-runtime install-agent.py) ---"
PARAMS = ["action", "task", "text", "extra"]
PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


def constants(src: str) -> dict:
    """Iter's top-level constants that bear on the adapter (literal assignments only)."""
    out = {}
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            value = node.value
            if isinstance(value, ast.Call) and getattr(value.func, "id", "") == "frozenset" and value.args:
                value = value.args[0]
            try:
                out[node.targets[0].id] = ast.literal_eval(value)
            except ValueError:
                pass
    return out


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


class Agent:
    """A scratch copy of the agent, called through its own iter.py --invoke."""

    def __init__(self, folder: Path, python: str):
        self.folder, self.python = folder, python
        self.src = (folder / "iter.py").read_text(encoding="utf-8", errors="replace")
        self.c = constants(self.src)
        self.tmp = Path(tempfile.mkdtemp(prefix="iter-conformance-"))
        shutil.copy2(folder / "iter.py", self.tmp / "iter.py")
        for rel in ("channels/hive.py", "tools/hive_task.py", "tools/send.py"):
            if (folder / rel).is_file():
                (self.tmp / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(folder / rel, self.tmp / rel)
        (self.tmp / ".hive").mkdir()
        (self.tmp / ".hive" / "config.json").write_text(json.dumps({"daemon": "sidecar"}))
        self.run = self.tmp / ".runtime" / "hive"

    def timeout(self, rel: str, function: str) -> float:
        """The limit Iter gives this call (VM2's split timeouts, else DYNAMIC_TIMEOUT)."""
        base = float(self.c.get("DYNAMIC_TIMEOUT", 5))
        path = Path(rel)
        if path.parent.name == "channels" and function in self.c.get("CHANNEL_TIMEOUT_FUNCTIONS", ()):
            return float(self.c.get("CHANNEL_TIMEOUT", base))
        if path.parent.name == "tools" and function == "run" and path.stem in self.c.get("CHANNEL_TIMEOUT_TOOLS", ()):
            return float(self.c.get("CHANNEL_TIMEOUT", base))
        return base

    def invoke(self, rel: str, function: str, *args, env_extra: dict | None = None, **kwargs) -> tuple[dict, float]:
        """One call the way Iter makes it; (Iter's result dict, seconds). Killed at Iter's limit."""
        limit = self.timeout(rel, function)
        fd, result = tempfile.mkstemp(dir=self.tmp, suffix=".json")
        os.close(fd)
        payload = self.tmp / (Path(result).stem + ".payload.json")
        payload.write_text(json.dumps({"args": args, "kwargs": kwargs}))
        env = {k: v for k, v in os.environ.items() if k != "ITER_REQUEST_IDS"}
        env.update(env_extra or {})
        started = time.monotonic()
        try:
            subprocess.run([self.python, "iter.py", "--invoke", str(self.tmp / rel), function, result, str(payload)],
                           cwd=self.tmp, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=limit, check=False)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"TIMEOUT after {limit:g}s"}, time.monotonic() - started
        took = time.monotonic() - started
        try:
            return json.loads(Path(result).read_text()), took
        except (OSError, ValueError):
            return {"ok": False, "error": "the --invoke worker wrote no result"}, took

    def outbox(self) -> list[dict]:
        out = []
        for p in sorted((self.run / "outbox").glob("[0-9]*")):
            out.append(json.loads(p.read_text()))
            p.unlink()
        return out

    def close(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


def check(folder: Path, python: str, prompt: Path | None) -> list[tuple[str, str, str]]:
    r: list[tuple[str, str, str]] = []
    add = lambda status, cid, detail: r.append((status, cid, detail))

    missing = [rel for rel in ("iter.py", "channels/hive.py", "tools/hive_task.py", ".hive/config.json")
               if not (folder / rel).is_file()]
    if missing:
        add(FAIL, "installed", "missing: " + ", ".join(missing) + " (install-agent.py iter --folder F)")
        return r
    add(PASS, "installed", "channel, tool and config are in place")

    src = (folder / "iter.py").read_text(encoding="utf-8", errors="replace")
    if '"--invoke"' not in src or "def dynamic_worker" not in src:
        add(FAIL, "invoke", "iter.py has no --invoke worker; this suite (and the adapter) assume Iter's dynamic calls")
        return r
    add(PASS, "invoke", "iter.py calls channels and tools through its --invoke worker")

    c = constants(src)
    max_tools = int(c.get("MAX_TOOLS", 30))
    tools = [p.stem for p in sorted((folder / "tools").glob("*.py")) if not p.name.startswith("_")]
    i = tools.index("hive_task")
    if i >= max_tools:
        add(FAIL, "tool_slot", f"hive_task is tool {i + 1} of {len(tools)}; Iter loads only the first {max_tools} "
            "(sorted by name), so the agent never sees it")
    elif len(tools) > max_tools:
        add(WARN, "tool_slot", f"{len(tools)} tools, Iter loads {max_tools}: hive_task is in, but "
            + ", ".join(tools[max_tools:]) + " are dropped")
    else:
        add(PASS, "tool_slot", f"hive_task is loaded ({len(tools)} of at most {max_tools} tools)")

    stale = [rel for rel in ("channels/hive.py", "tools/hive_task.py") if sha(folder / rel) != sha(HERE / rel)]
    try:
        conf = json.loads((folder / ".hive" / "config.json").read_text())
    except ValueError:
        conf = None
    sidecar = isinstance(conf, dict) and conf.get("daemon") == "sidecar"
    if stale:
        note = (" In sidecar mode only receive() and send() run in the agent; the sidecar runs its own copy."
                if sidecar else "")
        add(WARN, "adapter_copy", "differs from this adapter: " + ", ".join(stale)
            + "; reinstall with install-agent.py." + note)
    else:
        add(PASS, "adapter_copy", "the installed files are this adapter's")

    if conf is None or not isinstance(conf, dict):
        add(FAIL, "config", ".hive/config.json isn't a JSON object")
    elif sidecar:
        add(PASS, "config", "sidecar mode: the record key is outside the agent's reach")
    else:
        key = Path(conf.get("key_file", ""))
        if not key.is_file():
            add(FAIL, "config", f"spawned mode, but key_file {key} isn't there")
        elif stat.S_IMODE(key.stat().st_mode) & 0o077:
            add(WARN, "config", f"spawned mode: {key} is readable beyond its owner (record SPEC §6.6)")
        else:
            add(PASS, "config", "spawned mode, key readable only by its owner")

    prompt = prompt or folder / "prompt.txt"
    text = prompt.read_text(errors="replace") if prompt.is_file() else ""
    if BEGIN in text:
        add(PASS, "prompt_rule", f"the hive rule is in {prompt.name}")
    elif "hive_task" in text:
        add(WARN, "prompt_rule", f"{prompt.name} mentions hive_task but has no install-agent.py block "
            "(a hand-edited rule: install-agent.py won't update it)")
    else:
        add(WARN, "prompt_rule", f"no hive rule in {prompt} (install-agent.py iter --prompt FILE)")

    a = Agent(folder, python)
    try:
        res, _ = a.invoke("tools/hive_task.py", "__tool_metadata__")
        if not res.get("ok"):
            add(FAIL, "tool_metadata", f"Iter can't read hive_task: {res.get('error')} (is {python} the agent's "
                "interpreter, with its packages?)")
            return r
        meta = res["result"]
        params = meta.get("parameters") if isinstance(meta, dict) else None
        limit = c.get("MAX_TOOL_DESCRIPTION_CHARS")
        if params != PARAMS:
            add(FAIL, "tool_metadata", f"Iter sees parameters {params}, the adapter defines {PARAMS}")
        elif limit and len(meta.get("description", "")) > limit:
            add(WARN, "tool_metadata", f"Iter cuts hive_task's description at {limit} characters")
        else:
            add(PASS, "tool_metadata", "Iter reads hive_task's parameters and description whole")

        (a.run / "inbox").mkdir(parents=True, exist_ok=True)
        (a.run / "inbox" / "1-1").write_text("[conformance] a task for you")
        res, took = a.invoke("channels/hive.py", "receive")
        again, _ = a.invoke("channels/hive.py", "receive")
        limit = a.timeout("channels/hive.py", "receive")
        if not res.get("ok") or res.get("result") != "[conformance] a task for you":
            add(FAIL, "receive", f"receive() gave {res}")
        elif again.get("result"):
            add(FAIL, "receive", "a message came out of receive() twice")
        else:
            add(WARN if took > limit / 2 else PASS, "receive",
                f"delivered once in {took:.1f}s (Iter allows {limit:g}s)")

        if "__has_attrs__" in src:
            names = ["receive_request", "resume_all", "active_request_ids", "close_active", "expire_stale"]
            res, _ = a.invoke("channels/hive.py", "__has_attrs__", *names)
            if not (res.get("ok") and isinstance(res.get("result"), dict)):
                add(FAIL, "probe", f"Iter's channel probe fails on the hive channel ({res.get('error')}); with "
                    "request binding on, Iter then won't read it")
            else:
                has = [n for n, v in res["result"].items() if v]
                add(PASS, "probe", "Iter probes the hive channel: " + (", ".join(has) if has else
                    "no request-binding hooks, so Iter reads it with plain receive()"))

        res, took = a.invoke("tools/hive_task.py", "run", "progress", "", "conformance check", "")
        queued = a.outbox()
        limit = a.timeout("tools/hive_task.py", "run")
        if not (res.get("ok") and str(res.get("result", "")).startswith("QUEUED")) or len(queued) != 1:
            add(FAIL, "hive_task", f"the tool didn't queue the action: {res}")
        elif queued[0].get("action") != "progress" or queued[0].get("text") != "conformance check":
            add(FAIL, "hive_task", f"queued {queued[0]}")
        else:
            add(WARN if took > limit / 2 else PASS, "hive_task",
                f"queued in {took:.1f}s (Iter allows {limit:g}s for this tool)")

        if (a.tmp / "tools" / "send.py").is_file():
            res, _ = a.invoke("tools/send.py", "run", channel="hive", content="conformance note")
            queued = a.outbox()
            if res.get("ok") and str(res.get("result", "")).startswith("SUCCESS") and len(queued) == 1:
                add(PASS, "send", "send(channel=hive) posts a progress note")
            else:
                add(WARN, "send", f"send(channel=hive) doesn't reach the hive channel ({res.get('result') or res.get('error')}); "
                    "hive_task progress does")
            if "ITER_REQUEST_IDS" in a.tmp.joinpath("tools", "send.py").read_text(errors="replace"):
                res, _ = a.invoke("tools/send.py", "run", channel="hive", content="conformance note",
                                  env_extra={"ITER_REQUEST_IDS": json.dumps({"chat": "r1"})})
                queued = a.outbox()
                if res.get("ok") and str(res.get("result", "")).startswith("SUCCESS") and len(queued) == 1:
                    add(PASS, "send", "send(channel=hive) works while a chat request is bound to the turn")
                else:
                    add(WARN, "send", "while a chat request is bound to the turn, send(channel=hive) is refused ("
                        + str(res.get("result") or res.get("error"))[:160] + "); hive_task progress still works")
    finally:
        a.close()
    return r


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--folder", required=True)
    ap.add_argument("--python", default=sys.executable, help="the agent's interpreter (default: this one)")
    ap.add_argument("--prompt")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    results = check(Path(a.folder).resolve(), a.python, Path(a.prompt) if a.prompt else None)
    if a.json:
        print(json.dumps([{"status": s, "check": c, "detail": d} for s, c, d in results], indent=1))
    else:
        for s, c, d in results:
            print(f"{s:4}  {c:13} {d}")
    sys.exit(1 if any(s == FAIL for s, _, _ in results) else 0)


if __name__ == "__main__":
    main()
