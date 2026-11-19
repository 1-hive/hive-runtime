#!/usr/bin/env python3
"""Is the model tag working? Lists the record's questions, reports, results and reviews after a position, with the
model each one names (record A7, data.ext.model) or "-" when it names none, then a count per actor.

    HIVE_URL=… HIVE_ID=… HIVE_KEY_FILE=<a key of your own> model-tag-check.py [--after N]
    hive events --after N | model-tag-check.py -        # or from a saved event stream

--after defaults to 0; pass the position where the hive's A7 policy was applied (1-hive 2297, proto-hive 3242) to
see only events that could carry a model. An actor whose every line shows "-" isn't sending it: see README.md,
"Model accounts at their limit", for where each kind of agent gets its model.
"""
import argparse
import collections
import json
import subprocess
import sys

TYPES = {"task.blocked", "task.reported", "task.result_posted", "review.recorded"}

ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
ap.add_argument("source", nargs="?", help="'-' reads events (JSON lines) from stdin instead of `hive events`")
ap.add_argument("--after", type=int, default=0)
ap.add_argument("--hive", default="hive", help="the hive CLI (default: hive on PATH)")
a = ap.parse_args()

if a.source == "-":
    lines = sys.stdin
else:
    p = subprocess.run([a.hive, "events", "--after", str(a.after)], capture_output=True, text=True)
    if p.returncode:
        sys.exit(f"hive events failed: {p.stderr.strip()}")
    lines = p.stdout.splitlines()

per = collections.defaultdict(lambda: [0, 0])
for line in lines:
    try:
        e = json.loads(line)
    except ValueError:
        continue
    if e.get("type") not in TYPES or e.get("position", 0) <= a.after:
        continue
    model = ((e.get("data") or {}).get("ext") or {}).get("model")
    actor = (e.get("actor") or {}).get("id", "?")
    per[actor][0] += 1
    per[actor][1] += bool(model)
    print(f"{e.get('position'):>6}  {e.get('recorded_at', '')[:19]}  {e['type']:<19} {actor:<32} {model or '-'}")

if not per:
    print(f"no questions, reports, results or reviews after position {a.after} yet")
for actor, (n, tagged) in sorted(per.items()):
    print(f"{actor}: {tagged}/{n} name a model")
