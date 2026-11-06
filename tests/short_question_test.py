#!/usr/bin/env python3
"""short_question: the 500-character reason a blocked question gets in the record (the iter adapter)."""
import re
from pathlib import Path

src = (Path(__file__).resolve().parent.parent / "adapters/iter/channels/hive.py").read_text()
i = src.index("def short_question")
exec(src[i:src.index("\ndef ", i + 5)])

card = "Status: stuck\nBefore: Posting the result\n1. Log me in? yes: push\n2. Or you push?\n---\nlogs"
assert short_question(card) == "Before posting the result: 1. Log me in? yes: push 2. Or you push?", short_question(card)
assert short_question("plain question?\nOptions: a, b") == "plain question?"
assert short_question("Options\n1. a\n---\n") == "Options", "a numbered list with no status or before isn't a card"
long = "I need push access to publish the result. " * 20
r = short_question(long)
assert len(r) <= 500 and r.endswith("result. …"), r[-30:]
w = short_question("word " * 200)
assert len(w) <= 500 and w.endswith("word …"), w[-20:]
print("PASSED")
