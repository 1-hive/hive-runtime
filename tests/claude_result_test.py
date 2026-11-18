#!/usr/bin/env python3
"""claude-result.py and claude-failover.sh: a headless Claude Code reply comes back as its text, with the model that
wrote it (the last top-level assistant message's model, not a subagent's, nothing when unknown), and a call that ends
on an account limit moves to the next account. cos.sh chat prints that model as a "[[model: …]]" first line.

    python3 tests/claude_result_test.py
"""
import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULT, FAILOVER = ROOT / "claude-result.py", ROOT / "claude-failover.sh"


def stream(*events) -> str:
    return "".join(json.dumps(e) + "\n" for e in events)


def init(model="claude-opus-5-5"):
    return {"type": "system", "subtype": "init", "model": model}


def said(model, parent=None):
    return {"type": "assistant", "parent_tool_use_id": parent, "message": {"model": model, "content": []}}


def result(text, error=False, status=None):
    return {"type": "result", "subtype": "error" if error else "success", "is_error": error, "result": text,
            "api_error_status": status, "modelUsage": {}}


def run_result(raw: str) -> tuple[int, str, str | None]:
    with tempfile.TemporaryDirectory() as d:
        f, m = Path(d, "raw"), Path(d, "model")
        f.write_text(raw)
        p = subprocess.run(["python3", str(RESULT), str(f), "--model-file", str(m)], capture_output=True, text=True)
        return p.returncode, p.stdout, (m.read_text().strip() if m.exists() else None)


# The answering model: the last top-level assistant message, a subagent's skipped, a fallback model named.
rc, out, model = run_result(stream(init(), said("claude-opus-5-5"), said("claude-haiku-5-5", parent="toolu_1"),
                                   result("Done.")))
assert (rc, out, model) == (0, "Done.\n", "claude-opus-5-5"), (rc, out, model)
rc, out, model = run_result(stream(init(), said("claude-opus-5-5"), said("claude-sonnet-5-5"), result("Hi")))
assert model == "claude-sonnet-5-5", model
# Unknown: plain json output (no assistant messages), a synthetic message, or not JSON at all -> no model.
assert run_result(json.dumps(result("Plain")))[1:] == ("Plain\n", None)
assert run_result(stream(said("<synthetic>"), result("x")))[2] is None
assert run_result("claude: command crashed\n") == (0, "claude: command crashed\n", None)
# An account limit: exit 3, no model.
rc, out, model = run_result(stream(said("<synthetic>"), result("You've hit your limit · resets 5pm", True, 429)))
assert (rc, model) == (3, None), (rc, model)
# A reply that only mentions a limit is not one.
assert run_result(stream(said("claude-opus-5-5"), result("The usage limit is 5 per day.")))[0] == 0

# claude-failover.sh with a fake claude: account 1 limited, account 2 answers on Sonnet; the model file says so.
with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    fake = d / "claude"
    fake.write_text(f"""#!/bin/sh
cat > /dev/null
case "$*" in *stream-json*--verbose*) ;; *) echo "no stream-json: $*" >&2; exit 9 ;; esac
if [ "$CLAUDE_CODE_OAUTH_TOKEN" = one ]; then
  printf '%s\\n' '{json.dumps(said("<synthetic>"))}' '{json.dumps(result("You have hit your limit", True, 429))}'
  exit 1
fi
printf '%s\\n' '{json.dumps(said("claude-sonnet-5-5"))}' '{json.dumps(result("Answer from two"))}'
""")
    fake.chmod(0o755)
    (d / "t1").write_text("one\n")
    (d / "t2").write_text("two\n")
    env = dict(os.environ, CLAUDE_BIN=str(fake), CLAUDE_TOKEN_FILES=f"{d}/t1 {d}/t2", CLAUDE_FAILOVER_STATE=str(d / "st"),
               CLAUDE_FAILOVER_MODEL_FILE=str(d / "model"))
    p = subprocess.run([str(FAILOVER), "-p"], input="hello", capture_output=True, text=True, env=env)
    assert p.returncode == 0 and p.stdout == "Answer from two\n", (p.returncode, p.stdout, p.stderr)
    assert (d / "model").read_text().strip() == "claude-sonnet-5-5"
    assert any(f.name.startswith("claude-limited.t1") for f in (d / "st").iterdir())
    # No token files: one call as configured, still parsed, still a model.
    (d / "model").unlink()
    env.pop("CLAUDE_TOKEN_FILES")
    env["CLAUDE_CODE_OAUTH_TOKEN"] = "two"
    p = subprocess.run([str(FAILOVER), "-p"], input="hello", capture_output=True, text=True, env=env)
    assert p.returncode == 0 and p.stdout == "Answer from two\n", (p.returncode, p.stdout, p.stderr)
    assert (d / "model").read_text().strip() == "claude-sonnet-5-5"
    # Without CLAUDE_FAILOVER_MODEL_FILE (reviewer-claude): the text only, as before.
    env.pop("CLAUDE_FAILOVER_MODEL_FILE")
    p = subprocess.run([str(FAILOVER), "-p"], input="hello", capture_output=True, text=True, env=env)
    assert p.stdout == "Answer from two\n", p.stdout

cos = (ROOT / "cos.sh").read_text()
assert "[[model: %s]]" in cos and "claude-result.py" in cos
print("PASSED: the reply's text and the model that wrote it (subagents skipped, unknown left out); failover keeps it")
