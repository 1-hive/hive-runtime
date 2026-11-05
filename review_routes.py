"""Which long-lived reviewer gets a result: the router decides (hive-route rev 21, ROUTING.md §9.5).

Used by the dispatcher when HIVE_REVIEW_ROUTE_TABLE names a route table whose review routes name
their `actor` (a reviewer that runs all the time and takes its work from the record). For each
result it asks `hive-route decide` with:

- the task's facts: `kind: review` and the order's `Route facts:` line; the order's `Review tier:`
  line as the hint (it only raises the tier);
- the author: the table's route whose actor is the result's author (its family and model, for RT4);
- `unavailable`: every route whose actor the Floor (hive-bridge, HIVE_FLOOR_URL) shows can't run,
  offline or without beacon data (a reviewer not started yet), with the Floor's reason.

A decision names the actor to assign. A reviewer that has been unable to run for
HIVE_REVIEW_MOVE_SECONDS (600) while a result waits on it is decided again (`reason: reassign`),
and the result moves to the reviewer the router picks.

    HIVE_REVIEW_ROUTE_TABLE    the table (YAML or JSON); unset: no routing, the dispatcher uses HIVE_REVIEWERS
    HIVE_REVIEW_ROUTE_SOURCES  sources file with the qualifications file (hive-route canary accept --no-canary)
    HIVE_REVIEW_ROUTE_LOG      decision log (default <work>/.route/review-route-log.jsonl)
    HIVE_ROUTE_DIR             the hive-route checkout (paths.sh's default: <repos>/hive-route); its .venv if synced
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.request

import hive_paths as hp

TABLE = os.environ.get("HIVE_REVIEW_ROUTE_TABLE") or ""
SOURCES = os.environ.get("HIVE_REVIEW_ROUTE_SOURCES") or ""
LOG = os.environ.get("HIVE_REVIEW_ROUTE_LOG") or str(hp.WORK_DIR / ".route" / "review-route-log.jsonl")
ROUTE_DIR = os.environ.get("HIVE_ROUTE_DIR") or str(hp.REPOS_DIR / "hive-route")
FLOOR_URL = os.environ.get("HIVE_FLOOR_URL", "http://127.0.0.1:8476").rstrip("/")
MOVE_AFTER = int(os.environ.get("HIVE_REVIEW_MOVE_SECONDS") or 600)
DOWN = ("cantrun", "offline", "nodata")   # Floor states in which a reviewer takes no work (nodata: no beacon yet, e.g. not started)

FACT_VALUES = {"specification": ("explicit", "partial", "goal_only"), "verification": ("independent", "weak", "none"),
               "scope": ("single", "few", "many"), "consequence": ("reversible", "costly")}

_routes: tuple[float, dict] = (0.0, {})
down_since: dict[str, float] = {}     # actor -> when the Floor first showed it down (cleared when it's up)
floor_note: dict[str, str] = {}       # actor -> the Floor's reason


def enabled() -> bool:
    return bool(TABLE)


def hr(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    """A hive-route command (or its python): from the checkout's synced venv when there is one, which a
    service user can run without writing to a root-owned checkout; else through uv."""
    venv = os.path.join(ROUTE_DIR, ".venv", "bin")
    if os.path.exists(os.path.join(venv, args[0])):
        cmd = [os.path.join(venv, args[0]), *args[1:]]
    else:
        cmd = ["uv", "run", "-q", "--frozen", "--project", ROUTE_DIR, *args]
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=120)


def routes() -> dict:
    """The table's routes (validated by hive-route), reloaded when the file changes."""
    global _routes
    mtime = os.stat(TABLE).st_mtime
    if mtime != _routes[0]:
        r = hr("python", "-c", "import json, sys\nfrom hiveroute.table import load_table\n"
                               "print(json.dumps(load_table(sys.argv[1]).routes))", TABLE)
        if r.returncode != 0:
            raise RuntimeError(f"route table {TABLE}: {(r.stderr or r.stdout).strip()[-300:]}")
        _routes = (mtime, json.loads(r.stdout))
    return _routes[1]


def actors() -> set[str]:
    return {r["actor"] for r in routes().values() if r.get("actor") and "review" in r.get("kinds", ["review"])}


def read_floor(now: float | None = None) -> None:
    """Which agents the Floor shows down now, and since when (once per tick). A Floor that doesn't
    answer changes nothing: routing goes on with what was last seen."""
    now = now or time.time()
    try:
        with urllib.request.urlopen(FLOOR_URL + "/api/bots", timeout=10) as r:
            bots = json.load(r)["bots"]
    except Exception:
        return
    seen = set()
    for b in bots:
        st = b.get("now") or {}
        if st.get("state") in DOWN:
            seen.add(b["id"])
            down_since.setdefault(b["id"], now)
            floor_note[b["id"]] = (st.get("detail") or st.get("headline") or st["state"])[:300]
    for a in list(down_since):
        if a not in seen:
            down_since.pop(a, None); floor_note.pop(a, None)


def unavailable(extra: str | None = None) -> dict:
    """route id -> why, for every route whose actor the Floor shows down (and `extra`'s, being moved off)."""
    out = {}
    for rid, r in routes().items():
        a = r.get("actor")
        if a in down_since or (extra and a == extra):
            out[rid] = f"the Floor: {floor_note.get(a) or 'no verdict, being moved'}"
    return out


def order_facts(text: str) -> tuple[dict, dict | None]:
    """The order's `Route facts:` (only valid values; `unknown` and others left out) and its
    `Review tier:` as a hint."""
    facts: dict = {}
    m = re.search(r"^Route facts:(.*)$", text, re.M | re.I)
    for item in (m.group(1).split() if m else []):
        k, _, v = item.partition("=")
        if k == "leverage" and v.isdigit():
            facts[k] = int(v)
        elif v in FACT_VALUES.get(k, ()):
            facts[k] = v
    t = re.search(r"^Review tier: *(light|standard|strong)\b", text, re.M | re.I)
    hint = {"tier": t.group(1).lower(), "reason": "the order asks for this review tier"} if t else None
    return facts, hint


def order_text(pin: dict | None) -> str:
    """The order file at its pinned commit, from the hive's workspace repository ("" if unreadable)."""
    if not pin or not pin.get("path") or not pin.get("commit_oid"):
        return ""
    r = subprocess.run(["git", f"--git-dir={hp.WORKSPACE_REPO}", "show",
                        f"{pin['commit_oid'].split(':', 1)[-1]}:{pin['path']}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def author_of(actor: str | None) -> dict:
    for r in routes().values():
        if actor and r.get("actor") == actor:
            return {"family": r["family"], "model": r["model"]}
    return {"family": "unknown"}     # not in the table: RT4 can't rule anyone out (logged in the decision)


def decide(t: dict, reason: str = "new", moving_off: str | None = None) -> dict:
    """hive-route's decision for the review of t's current result. Raises if hive-route fails."""
    res = t["current_result"]
    facts, hint = order_facts(order_text(t.get("order")))
    req = {"task": t["id"], "attempt": f"{t['id']}.review.{res['event_id'][:13]}" + (".move" if moving_off else ""),
           "reason": reason, "facts": {"kind": "review", **facts}, "author": author_of(res.get("author")),
           "runtime": "long-lived"}
    if t.get("project"):
        req["project"] = t["project"]
    if hint:
        req["hint"] = hint
    un = unavailable(moving_off)
    if un:
        req["unavailable"] = un
    os.makedirs(os.path.dirname(LOG) or ".", exist_ok=True)
    args = ["hive-route", "decide", TABLE, "-", "--log", LOG] + (["--sources", SOURCES] if SOURCES else [])
    r = hr(*args, stdin=json.dumps(req))
    if r.returncode not in (0, 3):
        raise RuntimeError(f"hive-route decide: {(r.stderr or r.stdout).strip()[-300:]}")
    return json.loads(r.stdout)


def why(d: dict) -> str:
    rej = "; ".join(f"{k}: {', '.join(v)}" for k, v in (d.get("rejected") or {}).items())
    return f"{d['decision']} at tier {d.get('tier')}" + (f" ({rej})" if rej else "")


def should_move(actor: str, now: float | None = None) -> bool:
    """The assigned reviewer has been down on the Floor for MOVE_AFTER seconds."""
    since = down_since.get(actor)
    return since is not None and (now or time.time()) - since >= MOVE_AFTER
