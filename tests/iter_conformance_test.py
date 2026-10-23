"""adapters/iter/conformance.py against synthetic Iter folders: one shaped like VM2's Iter (channel probe,
split timeouts, a send tool that knows request binding), one with too many tools, one without --invoke.

    python tests/iter_conformance_test.py
"""
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
ADAPTER = HERE / "adapters" / "iter"
SUITE = ADAPTER / "conformance.py"

# The part of iter.py the adapter meets: constants, and the --invoke worker (as in VM2's Iter).
ITER = '''import importlib.util, inspect, json, sys
from pathlib import Path
MAX_TOOLS = {max_tools}
MAX_TOOL_DESCRIPTION_CHARS = 500
DYNAMIC_TIMEOUT = 5
CHANNEL_TIMEOUT = 30
CHANNEL_TIMEOUT_FUNCTIONS = frozenset({{"receive", "send", "__has_attrs__"}})
CHANNEL_TIMEOUT_TOOLS = frozenset({{"send"}})
ITER_REQUEST_BINDING = True

def dynamic_worker():
    path, function, result_path, payload_path = Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4]), Path(sys.argv[5])
    try:
        payload = json.loads(payload_path.read_text())
        spec = importlib.util.spec_from_file_location("_dynamic_" + path.stem, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if function == "__has_attrs__":
            result = {{n: callable(getattr(module, n, None)) for n in payload.get("args", [])}}
        elif function == "__tool_metadata__":
            result = {{"description": str(module.DESCRIPTION)[:MAX_TOOL_DESCRIPTION_CHARS],
                      "parameters": [p.name for p in inspect.signature(module.run).parameters.values()]}}
        else:
            result = getattr(module, function)(*payload.get("args", []), **payload.get("kwargs", {{}}))
            result = None if result is None else str(result)
        output = {{"ok": True, "result": result}}
    except BaseException as error:
        output = {{"ok": False, "error": f"{{type(error).__name__}}: {{error}}"}}
    result_path.write_text(json.dumps(output))

if len(sys.argv) > 1 and sys.argv[1] == "{flag}":
    dynamic_worker()
    sys.exit(0)
'''

# VM2's send tool, reduced: refuses a channel with no request bound to the turn.
SEND = '''import importlib.util, inspect, json, os
from pathlib import Path
DESCRIPTION = "Send a message through a communication channel."
def run(channel, content):
    path = Path("channels") / (channel + ".py")
    spec = importlib.util.spec_from_file_location("channel_" + channel, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    raw = os.environ.get("ITER_REQUEST_IDS")
    if raw is None:
        module.send(content)
        return "SUCCESS"
    rid = json.loads(raw).get(channel)
    if rid is None:
        return f"No {channel} request is bound to this turn, so there is nothing to answer."
    if "request_id" not in inspect.signature(module.send).parameters:
        return f"Channel {channel} cannot send to a bound request"
    module.send(content, request_id=rid)
    return "SUCCESS"
'''


def agent(root: Path, name: str, max_tools=30, extra_tools=(), flag="--invoke") -> Path:
    f = root / name
    (f / "channels").mkdir(parents=True)
    (f / "tools").mkdir()
    (f / "iter.py").write_text(ITER.format(max_tools=max_tools, flag=flag))
    (f / "tools" / "send.py").write_text(SEND)
    for t in extra_tools:
        (f / "tools" / f"{t}.py").write_text('DESCRIPTION = "x"\ndef run(a):\n    return a\n')
    spec = importlib.util.spec_from_file_location("ia", HERE / "adapters" / "install-agent.py")
    ia = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ia)
    shutil.copy2(ADAPTER / "channels" / "hive.py", f / "channels" / "hive.py")
    shutil.copy2(ADAPTER / "tools" / "hive_task.py", f / "tools" / "hive_task.py")
    (f / ".hive").mkdir()
    (f / ".hive" / "config.json").write_text(json.dumps({"daemon": "sidecar"}))
    (f / "prompt.txt").write_text("You are a bot.\n\n" + ia.BEGIN + "\nUse hive_task.\n" + ia.END + "\n")
    return f


def run(folder: Path) -> tuple[int, dict]:
    p = subprocess.run([sys.executable, str(SUITE), "--folder", str(folder), "--json"], capture_output=True, text=True, check=False)
    rows = json.loads(p.stdout)
    by = {}
    for r in rows:
        by.setdefault(r["check"], []).append((r["status"], r["detail"]))
    return p.returncode, by


def main():
    root = Path(tempfile.mkdtemp())
    try:
        code, by = run(agent(root, "vm2"))
        assert code == 0, by
        for c in ("installed", "invoke", "tool_slot", "adapter_copy", "config", "prompt_rule", "tool_metadata",
                  "receive", "probe", "hive_task"):
            assert by[c][0][0] == "PASS", (c, by[c])
        assert "Iter allows 5s for this tool" in by["hive_task"][0][1], by["hive_task"]
        assert "Iter allows 30s" in by["receive"][0][1], by["receive"]
        assert [s for s, _ in by["send"]] == ["PASS", "WARN"], by["send"]     # refused while bound to a chat request
        assert not list((root / "vm2" / ".runtime").glob("**/outbox/[0-9]*")), "touched the agent's own queue"

        code, by = run(agent(root, "crowded", max_tools=3, extra_tools=("alpha", "beta", "gamma")))
        assert code == 1 and by["tool_slot"][0][0] == "FAIL" and "tool 4 of 5" in by["tool_slot"][0][1], by

        code, by = run(agent(root, "old", flag="--run-dynamic"))
        assert code == 1 and by["invoke"][0][0] == "FAIL", by

        f = agent(root, "stale")
        (f / "channels" / "hive.py").write_text((f / "channels" / "hive.py").read_text() + "\n# local edit\n")
        code, by = run(f)
        assert code == 0 and by["adapter_copy"][0][0] == "WARN" and "sidecar" in by["adapter_copy"][0][1], by
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("ok")


if __name__ == "__main__":
    main()
