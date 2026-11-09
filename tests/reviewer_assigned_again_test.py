#!/usr/bin/env python3
"""reviewer_runner.assigned_again: a review assigned to the same reviewer again after its own review runs again. On 11-09
the CoS sent reviewer.claude's two-sentence review of t-2026-11-09-roman-issues-rerun-dedupe (position 1712) back to
reviewer.claude (1713), and the runner, which skipped every task whose latest review was its own, never ran it.

    python3 tests/reviewer_assigned_again_test.py
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
tree = ast.parse((ROOT / "adapters/reviewer/reviewer_runner.py").read_text())
ns = {}
keep = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "assigned_again"]
exec(compile(ast.Module(body=keep, type_ignores=[]), "reviewer_runner", "exec"), ns)
again = ns["assigned_again"]

ev = lambda pos, typ, **data: {"position": pos, "type": typ, "data": data}
lr = {"position": 1712, "reviewer": "reviewer.claude", "verdict": "passed"}
seen = [ev(1696, "task.result_posted"), ev(1697, "review.assigned", reviewer="reviewer.claude"),
        ev(1712, "review.recorded"), ev(1713, "review.assigned", reviewer="reviewer.claude")]
assert again(seen, lr, "reviewer.claude")
assert not again(seen[:3], lr, "reviewer.claude")                      # the first assignment came before its review
assert not again(seen[:3] + [ev(1713, "review.assigned", reviewer="reviewer.astra")], lr, "reviewer.claude")
assert not again(seen + [ev(1720, "review.recorded")], {**lr, "position": 1720}, "reviewer.claude")   # reviewed again
print("PASSED: a review sent back to the same reviewer runs again, once")
