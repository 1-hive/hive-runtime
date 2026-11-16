#!/usr/bin/env python3
"""The operator's own words to the chief of staff, logged where agents can't write (WORK_DIR/.telegram/
operator-words.jsonl, the Telegram bridge's folder): the bridge adds what the operators write in Telegram, and
`op chat` what the operator's host session tells the chief of staff. A goal whose `hive-request` block quotes them
starts without an Approve tap (hive-bridge); a goal of the chief of staff's own initiative still waits for one.

    operator_words.py add --via "host session" [--from NAME] < message

One JSON line per message: {"at": ISO time (UTC), "via", "from", "text"}.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

import hive_paths as hp

LOG = hp.WORK_DIR / ".telegram" / "operator-words.jsonl"


def add(text: str, via: str, who: str = "operator") -> None:
    if not text.strip():
        return
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps({"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "via": via,
                            "from": who, "text": text}) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("--via", required=True)
    a.add_argument("--from", dest="who", default="operator")
    args = ap.parse_args()
    add(sys.stdin.read(), args.via, args.who)


if __name__ == "__main__":
    main()
