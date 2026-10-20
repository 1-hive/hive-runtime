#!/usr/bin/env python3
"""How much of a long-lived agent's model time went to record tasks, and how much to everything else.

    time_split.py --root <the agent's Iter folder> [--hook <route-hook.jsonl>] [--days 1] [--json]

Episodes come from the agent's hive-route hook log (`route-hook.jsonl` in the agent's folder: one
`routed` and one `ended` entry per episode, with times). An episode counts as record work when it
started inside a window in which the agent held an accepted record task (the sidecar notes
`accepted_at` and `result_at` per task in .runtime/hive/tasks/). Everything else is chat and the
agent's own loop. Ben's ask (2026-10-20): the chief of staff should know how much time each agent
has for assigned work; it varies a lot between agents.
"""
import argparse
import datetime as dt
import json
from pathlib import Path


def parse_time(s: str) -> float:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def windows(root: Path) -> list[tuple[float, float | None]]:
    out = []
    for f in sorted((root / ".runtime" / "hive" / "tasks").glob("*.json")):
        try:
            d = json.loads(f.read_text())
        except ValueError:
            continue
        start = d.get("accepted_at")
        if start:
            out.append((start, d.get("result_at")))
    return out


def episodes(hook: Path) -> list[dict]:
    routed, out = {}, []
    for line in hook.read_text().splitlines() if hook.is_file() else []:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("event") == "routed" and e.get("episode"):
            routed[e["episode"]] = e
        elif e.get("event") == "ended" and e.get("episode") in routed:
            r = routed.pop(e["episode"])
            out.append({"episode": e["episode"], "start": parse_time(r["at"]), "end": parse_time(e["at"]),
                        "model": r.get("model"), "tier": r.get("tier")})
    return out


def split(root: Path, hook: Path, days: float) -> dict:
    since = dt.datetime.now(dt.UTC).timestamp() - days * 86400
    wins = windows(root)
    rec = oth = 0.0
    n_rec = n_oth = 0
    for ep in episodes(hook):
        if ep["start"] < since:
            continue
        dur = max(0.0, ep["end"] - ep["start"])
        if any(a <= ep["start"] <= (b or float("inf")) for a, b in wins):
            rec += dur
            n_rec += 1
        else:
            oth += dur
            n_oth += 1
    total = rec + oth
    return {"agent_dir": str(root), "days": days, "record_seconds": int(rec), "other_seconds": int(oth),
            "record_episodes": n_rec, "other_episodes": n_oth,
            "record_share": round(rec / total, 2) if total else None}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", required=True)
    ap.add_argument("--hook")
    ap.add_argument("--days", type=float, default=1)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    root = Path(a.root)
    r = split(root, Path(a.hook) if a.hook else root / "route-hook.jsonl", a.days)
    if a.json:
        print(json.dumps(r))
    else:
        share = f"{int(r['record_share'] * 100)}%" if r["record_share"] is not None else "n/a"
        print(f"{root.name}: last {a.days:g} day(s): record work {r['record_seconds'] // 60} min in {r['record_episodes']} "
              f"episodes, other {r['other_seconds'] // 60} min in {r['other_episodes']} episodes; record share {share}")


if __name__ == "__main__":
    main()
