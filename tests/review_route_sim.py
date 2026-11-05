"""Routed reviews (review_routes.py) against a stubbed record and Floor, with the real hive-route.

    python3 tests/review_route_sim.py      (HIVE_ROUTE_DIR: a hive-route checkout, rev 21 or later;
                                            default ../hive-route beside this repository)

Checks: the order's Route facts and Review tier set the review's tier and so the reviewer; a reviewer the
Floor shows can't run is passed over, and RT4 by model lets a different model by the author's maker review;
no reviewer able to run: nothing assigned, asked again later; a result waiting on a reviewer that has been
down past HIVE_REVIEW_MOVE_SECONDS is moved, not before; a router that fails falls back to HIVE_REVIEWERS.
"""
import importlib.util, io, json, os, subprocess, sys, tempfile, time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp())
ROUTE_DIR = os.environ.get("HIVE_ROUTE_DIR") or str(HERE.parent / "hive-route")

TABLE = {
    "format": "hive-route.table/1", "version": 1, "review_distinct": "model",
    "fact_defaults": {"author_tier": "light"},
    "pools": {"openai": {"kind": "subscription", "limits": []}, "claude": {"kind": "subscription", "limits": []}},
    "routes": {
        "astra": {"tier": "strong", "pool": "openai", "family": "gpt", "model": "gpt-6-astra", "tools": True,
                  "kinds": ["review"], "actor": "reviewer.astra"},
        "fable": {"tier": "strong", "pool": "claude", "family": "claude", "model": "claude-fable-5-1", "tools": True,
                  "kinds": ["review"], "actor": "reviewer.claude"},
        "fable-std": {"tier": "standard", "pool": "claude", "family": "claude", "model": "claude-fable-5-1",
                      "tools": True, "kinds": ["review"], "actor": "reviewer.claude"},
        "astra-std": {"tier": "standard", "pool": "openai", "family": "gpt", "model": "gpt-6-astra", "tools": True,
                      "kinds": ["review"], "actor": "reviewer.astra"},
        "light": {"tier": "light", "pool": "claude", "family": "claude", "model": "claude-haiku-5-5", "tools": True,
                  "kinds": ["review"], "actor": "reviewer.light"},
        "bot": {"tier": "strong", "pool": "claude", "family": "claude", "model": "claude-opus-5-5", "tools": True,
                "actor": "worker.bot"},
    },
    "tiers": {"strong": ["astra", "fable"], "standard": ["fable-std", "astra-std"], "light": ["light"]},
}
(TMP / "table.json").write_text(json.dumps(TABLE))
(TMP / "sources.json").write_text(json.dumps({"format": "hive-route.sources/1", "qualifications": str(TMP / "q.json")}))
hr = ["uv", "run", "-q", "--frozen", "--project", ROUTE_DIR, "hive-route"]
for rid in ("astra", "fable", "fable-std", "astra-std", "light"):
    subprocess.run(hr + ["canary", "accept", str(TMP / "table.json"), rid, "--no-canary", "--by", "sim", "--reason",
                         "test", "--sources", str(TMP / "sources.json"), "--log", str(TMP / "accept.jsonl")],
                   check=True, capture_output=True)

# the workspace: one order per tier
ws = TMP / "ws"; subprocess.run(["git", "init", "-q", str(ws)], check=True)
ORDERS = {
    "strong.md": "Route facts: specification=explicit verification=independent scope=few consequence=reversible leverage=0\nReview tier: strong\n",
    "standard.md": "Route facts: specification=explicit verification=weak scope=many consequence=reversible leverage=0\n",
    "light.md": "Route facts: specification=explicit verification=independent scope=single consequence=reversible leverage=0\n",
}
for n, txt in ORDERS.items():
    (ws / n).write_text(txt)
subprocess.run(["git", "-C", str(ws), "add", "."], check=True)
subprocess.run(["git", "-C", str(ws), "-c", "user.name=s", "-c", "user.email=s@s", "commit", "-qm", "orders"], check=True)
SHA = subprocess.run(["git", "-C", str(ws), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()

os.environ.update({"HIVE_LONG_LIVED_ACTORS": "worker.*,reviewer.*", "HIVE_WORK_DIR": str(TMP / "work"),
                   "HIVE_REVIEWERS": "reviewer.astra", "HIVE_REVIEW_ROUTE_TABLE": str(TMP / "table.json"),
                   "HIVE_REVIEW_ROUTE_SOURCES": str(TMP / "sources.json"), "HIVE_ROUTE_DIR": ROUTE_DIR,
                   "HIVE_WORKSPACE_REPO": str(ws / ".git"), "HIVE_REVIEW_MOVE_SECONDS": "600"})
sys.path.insert(0, str(HERE))


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m; spec.loader.exec_module(m); return m


rr = load("review_routes"); load("supervisor"); dis = load("dispatcher")
emitted, logs = [], []
dis.log = logs.append
EVENTS: dict = {}


def fake_hive(*a):
    if a[0] == "task": return json.dumps({"events": EVENTS.get(a[1], [])})
    if a[0] == "emit": emitted.append((a[1], a[3], json.loads(a[a.index("--data") + 1]))); return json.dumps({"position": 1})
    raise AssertionError(a)


dis.hive = fake_hive
FLOOR: dict = {}


class Resp(io.StringIO):
    def __enter__(self): return self
    def __exit__(self, *a): pass


rr.urllib.request.urlopen = lambda url, timeout=0: Resp(json.dumps({"bots": [
    {"id": a, "now": {"state": s, "detail": f"{a} {s}"}} for a, s in FLOOR.items()]}))


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f": {detail}")); cond or sys.exit(1)


def task(tid, order):
    return {"id": tid, "status": "in_review", "owner": "worker.bot", "project": "p", "ext": {},
            "order": {"path": order, "commit_oid": f"git:{SHA}"},
            "current_result": {"event_id": f"ev-{tid}-0000000000", "author": "worker.bot"}}


def run(t, now=None):
    emitted.clear()
    rr.read_floor(now)
    dis.handle(t, {"tasks": {}, "goals": {}}, False, RETRY)
    return [e[2]["reviewer"] for e in emitted if e[0] == "review.assigned"]


RETRY: dict = {}
check("strong order, all up: Astra, by the router", run(task("s1", "strong.md")) == ["reviewer.astra"]
      and "routed: astra, tier strong" in logs[-1], logs[-3:])
FLOOR["reviewer.astra"] = "cantrun"
check("strong order, Astra can't run: Fable (another model by the author's maker)",
      run(task("s2", "strong.md")) == ["reviewer.claude"], logs[-3:])
check("standard order: Fable first", run(task("m1", "standard.md")) == ["reviewer.claude"], logs[-3:])
check("light order: the light reviewer", run(task("l1", "light.md")) == ["reviewer.light"], logs[-3:])
FLOOR["reviewer.light"] = "nodata"
check("a reviewer with no beacon data yet is passed over", run(task("l2", "light.md")) == [] and "asking again" in logs[-1], logs[-2:])
del FLOOR["reviewer.light"]
FLOOR["reviewer.claude"] = "offline"
check("strong order, both down: nothing assigned", run(task("s3", "strong.md")) == [], logs[-3:])
check("... asked again later", RETRY.get("ev-s3-0000000000", 0) > time.time() and "asking again" in logs[-1], logs[-2:])
FLOOR.clear(); rr.read_floor()

# moving a waiting review
t = task("mv", "strong.md")
EVENTS["mv"] = [{"type": "review.assigned", "recorded_at": "2026-11-05T00:00:00Z",
                 "data": {"reviewer": "reviewer.astra", "result_event": "ev-mv-0000000000"}}]
FLOOR["reviewer.astra"] = "cantrun"
now = time.time()
check("assigned reviewer down under the wait: kept", run(t, now) == [], logs[-3:])
rr.down_since["reviewer.astra"] = now - 700   # the Floor has shown it down for 700 s
check("down past HIVE_REVIEW_MOVE_SECONDS: moved to Fable", run(t) == ["reviewer.claude"]
      and "moved off reviewer.astra" in logs[-1], logs[-3:])
EVENTS["mv"].append({"type": "review.recorded", "data": {"result_event": "ev-mv-0000000000"}})
check("a recorded verdict: never moved", run(t) == [], logs[-3:])
FLOOR.clear(); rr.read_floor()

# a router that fails
rr.TABLE = str(TMP / "missing.json")
check("router fails: HIVE_REVIEWERS as before", run(task("f1", "strong.md")) == ["reviewer.astra"]
      and any("routing failed" in m for m in logs[-3:]), logs[-3:])
print("PASSED")
