#!/usr/bin/env python3
"""reviewer_runner.limit_line: a review that ends on its model account's usage, spend or credit limit is reported as
that (the beacon's problem, so the Floor shows the reviewer can't run and alerts once), not as a bare "no verdict".
supervisor.py's CAPACITY_SIGNS cover the same wordings.

    python3 tests/reviewer_limit_test.py
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
src = (ROOT / "adapters/reviewer/reviewer_runner.py").read_text()
tree = ast.parse(src)
ns = {}
keep = [n for n in tree.body if (isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "LIMIT_SIGNS" for t in n.targets))
        or (isinstance(n, ast.FunctionDef) and n.name == "limit_line")]
exec(compile(ast.Module(body=keep, type_ignores=[]), "reviewer_runner", "exec"), ns)
limit_line = ns["limit_line"]

claude = "Reading the kickoff...\nYou've hit your individual spend limit · resets 2pm (America/Los_Angeles)\n"
assert limit_line(claude) == "You've hit your individual spend limit · resets 2pm (America/Los_Angeles)"
assert limit_line("Claude usage limit reached. Your limit will reset at 5pm\n")
assert limit_line("API Error: Credit balance is too low to access the Anthropic API.\n")
codex = "ERROR: Your workspace is out of credits. Ask your workspace owner to refill in order to continue.\ntokens used\n47,632\n"
assert limit_line(codex).startswith("ERROR: Your workspace is out of credits")
assert limit_line("Traceback ...\nAssertionError: tests failed\n") is None
assert limit_line("") is None

sup = (ROOT / "supervisor.py").read_text()
caps = ast.literal_eval(next(n.value for n in ast.parse(sup).body if isinstance(n, ast.Assign)
                             and getattr(n.targets[0], "id", "") == "CAPACITY_SIGNS"))
for text in (claude, "Claude usage limit reached", "Credit balance is too low", codex):
    assert any(s in text.lower() for s in caps), text
print("PASSED: usage, spend and credit limits are named as such (runner beacon, supervisor capacity)")
