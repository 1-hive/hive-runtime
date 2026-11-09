#!/usr/bin/env python3
"""reviewer_runner.thin_review: a verdict with (almost) nothing before it isn't recorded as a review. On 11-09
reviewer.claude's saved output was two sentences and "VERDICT: passed": a background monitor woke the session after
its 5,333-character review, and `claude -p` keeps only the last message.

    python3 tests/reviewer_thin_review_test.py
"""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
tree = ast.parse((ROOT / "adapters/reviewer/reviewer_runner.py").read_text())
ns = {"re": re}
keep = [n for n in tree.body if (isinstance(n, ast.Assign) and any(getattr(t, "id", "") in ("VERDICT", "MIN_REVIEW")
                                                                   for t in n.targets))
        or (isinstance(n, ast.FunctionDef) and n.name == "thin_review")]
exec(compile(ast.Module(body=keep, type_ignores=[]), "reviewer_runner", "exec"), ns)
thin = ns["thin_review"]

seen = ("The monitor has expired after delivering the last results. No further runs are pending; the review above is "
        "complete and final.\n\nVERDICT: passed\n")
assert thin(seen)
assert thin("VERDICT: failed\n")
full = ("## Findings\n" + "".join(f"{i}. Re-ran check {i}: the drafts match the order's criterion {i}; "
                                  f"evidence in out/{i}.txt.\n" for i in range(1, 12)) + "\nVERDICT: passed\n")
assert not thin(full)
assert not thin("no verdict here")           # no verdict: the runner's own no-verdict path handles it
# the last verdict counts: a long review that quotes an earlier "VERDICT" line still passes
assert not thin("VERDICT: failed\n" + full)
print("PASSED: a verdict without findings or checks isn't recorded as a review")
