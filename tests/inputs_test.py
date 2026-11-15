#!/usr/bin/env python3
"""Host inputs (inputs.py): the files a goal's approval grants its tasks, staged once as a read-only copy that every
task of the goal and its reviews read. Before this, kg-1-options (2026-11-14) blocked twice on `needs: access`: once
for Kev's weights, and again because its reviewer couldn't see the copy made for the worker.

Checks: the block's syntax; only the goal document as approved counts; paths that would expose keys, logins or the
hive's state are refused, also through links; the copy (read-only, links kept inside an input, links out of it
dereferenced or left out, key-like files left out, sha256 manifest, made once); the launcher's command (link and
KICKOFF section); the sweeper removing a settled goal's copy.

    python3 tests/inputs_test.py
"""
import datetime as dt
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

T = Path(os.path.realpath(tempfile.mkdtemp()))
HOME = T / "home"
HOME.mkdir()
os.environ.update(HOME=str(HOME), HIVE_WORK_DIR=str(HOME / "work" / "1hive"), HIVE_CONFIG_DIR=str(HOME / ".config" / "hive"),
                  HIVE_WORKSPACE_REPO=str(T / "workspace"))
os.environ.pop("HIVE_INPUT_ROOTS", None)
os.environ.pop("HIVE_INPUTS_MAX_BYTES", None)
HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


inp = load("inputs")
WORK = HOME / "work" / "1hive"
for d in (WORK / ".route", WORK / ".ports", HOME / ".ssh", HOME / ".config" / "hive"):
    d.mkdir(parents=True)
(HOME / ".ssh" / "id_ed25519").write_text("PRIVATE")
fails = []


def check(cond, what):
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        fails.append(what)


def refused(fn, *a):
    try:
        fn(*a)
    except inp.Refused as e:
        return str(e)
    return None


# ---- the block ---------------------------------------------------------------------------------------------------
DOC = """# Goal

Text.

```hive-inputs
# a comment
~/opt/kev/runs/run-1    Kev's served run, for the CPU test
/abs/path/x             the other thing
```
"""
check(inp.parse(DOC) == [("~/opt/kev/runs/run-1", "Kev's served run, for the CPU test"), ("/abs/path/x", "the other thing")],
      "parse: path and why per line, comments skipped")
check(inp.parse("# Goal\n\nno block\n") == [], "parse: no block, no inputs")
check("isn't closed" in (refused(inp.parse, "```hive-inputs\n~/a  why\n") or ""), "parse: an unclosed block is refused")
check("more than one" in (refused(inp.parse, DOC + "\n" + DOC) or ""), "parse: two blocks are refused")
check("say why" in (refused(inp.parse, "```hive-inputs\n~/a\n```\n") or ""), "parse: a path without why is refused")
check("starts with" in (refused(inp.parse, "```hive-inputs\nopt/a  why\n```\n") or ""), "parse: a relative path is refused")
nine = "```hive-inputs\n" + "".join(f"~/a{i}  why\n" for i in range(9)) + "```\n"
check("at most 8" in (refused(inp.parse, nine) or ""), "parse: more than 8 inputs are refused")
check("twice" in (refused(inp.parse, "```hive-inputs\n~/a  x\n~/a  y\n```\n") or ""), "parse: a path listed twice is refused")

# ---- which version counts ----------------------------------------------------------------------------------------
P1, P2, P3 = ({"repository": "workspace", "commit_oid": f"sha1:{c * 40}", "path": "g.md", "version": 2} for c in "abc")
EVENTS = [{"position": 10, "type": "goal.proposed", "refs": [{"rel": "goal", "pin": P1}]},
          {"position": 11, "type": "goal.revised", "refs": [{"rel": "goal", "pin": P2}]},
          {"position": 12, "type": "goal.approved", "refs": []},
          {"position": 13, "type": "goal.revised", "refs": [{"rel": "goal", "pin": P3}]}]
check(inp.approved(EVENTS) == (P2, 12), "approved: the last goal pin before goal.approved; a later revision doesn't count")
check(inp.approved(EVENTS[:2]) is None, "approved: a goal not yet approved grants nothing")

# ---- what may be granted -----------------------------------------------------------------------------------------
data = HOME / "opt" / "kev"
(data / "runs" / "run-1").mkdir(parents=True)
(data / "runs" / "run-1" / "adapter.safetensors").write_bytes(b"A" * 1000)
os.chmod(data / "runs" / "run-1" / "adapter.safetensors", 0o600)   # mode 600, as Kev's was: the copy is readable
(data / "runs" / "run-1" / "token").write_text("hf_secret")
(data / "runs" / "run-1" / "agent.key").write_text("k")
(data / "runs" / "run-1" / "to-ssh").symlink_to(HOME / ".ssh" / "id_ed25519")
(data / "runs" / "run-1" / "dangling").symlink_to(HOME / "nowhere")
(HOME / "sneaky").symlink_to(HOME / ".ssh")
check(inp.resolve("~/opt/kev/runs/run-1") == data / "runs" / "run-1", "resolve: a folder in the operator's home")
for path, word in [("~/.ssh", "inside"), ("~/.config/hive", "inside"), ("~/sneaky", "inside"), (str(HOME), "whole allowed root"),
                   ("~/work/1hive", "contains"), ("~/work/1hive/.route", "inside"), ("~/work", "contains"),
                   ("/etc/passwd", "outside"), ("~/missing", "no such"), (str(T), "outside")]:
    why = refused(inp.resolve, path) or ""
    check(word in why, f"resolve: {path} refused ({why[:70]})")
os.environ["HIVE_INPUT_ROOTS"] = f"{HOME}:{T / 'extra'}"
(T / "extra" / "d").mkdir(parents=True)
check(inp.resolve(str(T / "extra" / "d")) == T / "extra" / "d", "resolve: HIVE_INPUT_ROOTS adds roots")
os.environ.pop("HIVE_INPUT_ROOTS")

# ---- the copy ----------------------------------------------------------------------------------------------------
# An HF cache as Kev's is laid out: the snapshot's files link to the model's blobs, which link to the hub's own store.
hub = HOME / ".cache" / "huggingface" / "hub"
model = hub / "models--Q--B"
(model / "snapshots" / "rev1").mkdir(parents=True)
(model / "blobs").mkdir()
(hub / "blobs" / "19").mkdir(parents=True)
(hub / "blobs" / "19" / "real").write_bytes(b"W" * 5000)
(model / "blobs" / "shard").symlink_to("../../blobs/19/real")
(model / "blobs" / "cfg").write_text('{"a": 1}')
(model / "snapshots" / "rev1" / "model.safetensors").symlink_to("../../blobs/shard")
(model / "snapshots" / "rev1" / "config.json").symlink_to("../../blobs/cfg")
(hub.parent / "token").write_text("hf_secret")
check("contains" in (refused(inp.resolve, "~/.cache/huggingface") or ""), "resolve: an HF cache holding the HF token is refused")
single = HOME / "notes.txt"
single.write_text("one file")

INPUTS = [("~/opt/kev/runs/run-1", "Kev's run"), ("~/.cache/huggingface/hub/models--Q--B", "the base"),
          ("~/notes.txt", "a single file")]
rec = inp.stage("g1", 12, P2, INPUTS)
dest = Path(rec["dir"])
check(dest.parent == WORK / ".inputs" / "g1", "stage: under <work>/.inputs/<goal>/")
check([i["name"] for i in rec["inputs"]] == ["run-1", "models--Q--B", "notes.txt"], "stage: one entry per input, named by basename")
check((dest / "run-1" / "adapter.safetensors").read_bytes() == b"A" * 1000, "stage: a mode-600 file is copied")
check(not (dest / "run-1" / "token").exists() and not (dest / "run-1" / "agent.key").exists(), "stage: key-like files are left out")
check(not (dest / "run-1" / "to-ssh").exists() and not os.path.lexists(dest / "run-1" / "to-ssh"), "stage: a link into ~/.ssh is left out")
left = {r for r, _ in rec["inputs"][0]["skipped"]}
check(left == {"run-1/token", "run-1/agent.key", "run-1/to-ssh", "run-1/dangling"}, f"stage: what was left out is listed ({sorted(left)})")
snap = dest / "models--Q--B" / "snapshots" / "rev1" / "model.safetensors"
check(snap.is_symlink() and os.readlink(snap) == "../../blobs/shard", "stage: a link inside the input stays a link")
check(snap.read_bytes() == b"W" * 5000, "stage: ... and reads the copied blob")
shard = dest / "models--Q--B" / "blobs" / "shard"
check(shard.is_file() and not shard.is_symlink(), "stage: a link out of the input (the hub's store) is copied as a file")
check(rec["inputs"][1]["bytes"] == 5000 + len('{"a": 1}'), "stage: the HF model is copied once (no snapshot duplicates)")
check((dest / "notes.txt").read_text() == "one file", "stage: a single-file input")
modes = {oct(os.stat(p, follow_symlinks=False).st_mode & 0o777) for p in dest.rglob("*") if not p.is_symlink()}
check(modes <= {"0o444", "0o555"} and oct(dest.stat().st_mode & 0o777) == "0o555", f"stage: read-only files and folders ({modes})")
man = dict(reversed(l.split("  ", 1)) for l in (dest / "MANIFEST.sha256").read_text().splitlines())
check(man["run-1/adapter.safetensors"] == hashlib.sha256(b"A" * 1000).hexdigest() and len(man) == 4,
      "stage: MANIFEST.sha256 has every copied file")
check(json.loads((dest / "STAGED.json").read_text())["approved_at"] == 12 and "left out" in (dest / "README.md").read_text(),
      "stage: STAGED.json and README.md")
(data / "runs" / "run-1" / "adapter.safetensors").write_bytes(b"B" * 10)
again = inp.stage("g1", 12, P2, INPUTS)
check(again == rec and (dest / "run-1" / "adapter.safetensors").read_bytes() == b"A" * 1000,
      "stage: made once; later launches read the same copy even if the original changed")
inp.MAX_BYTES = 100
check("over the" in (refused(inp.stage, "g2", 20, P1, INPUTS) or ""), "stage: over the size limit is refused")
check(not any((WORK / ".inputs" / "g2").glob(".tmp-*")), "stage: a refused stage leaves nothing half-made")
inp.MAX_BYTES = 64 * 10**9

# ---- the launcher's command --------------------------------------------------------------------------------------
ws = T / "workspace"
git = lambda *a: subprocess.run(["git", "-C", str(ws), *a], check=True, capture_output=True, text=True).stdout.strip()
ws.mkdir()
git("init", "-q")
os.environ.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
(ws / "g.md").write_text("# G\n\n```hive-inputs\n~/opt/kev/runs/run-1  Kev's run\n```\n")
git("add", "g.md")
git("commit", "-q", "-m", "approved version")
A = git("rev-parse", "HEAD")
(ws / "g.md").write_text("# G\n\n```hive-inputs\n~/opt/kev/runs/run-1  Kev's run\n~/notes.txt  added after approval\n```\n")
git("commit", "-q", "-am", "revised after approval")
B = git("rev-parse", "HEAD")
pin = lambda c: {"repository": "workspace", "commit_oid": f"sha1:{c}", "path": "g.md", "version": 2}
GOAL = {"goal": {"id": "g3", "goal_pin": pin(B)},
        "events": [{"position": 30, "type": "goal.proposed", "refs": [{"rel": "goal", "pin": pin(A)}]},
                   {"position": 31, "type": "goal.approved", "refs": []},
                   {"position": 32, "type": "goal.revised", "refs": [{"rel": "goal", "pin": pin(B)}]}]}
TASKS = {"t3": {"task": {"id": "t3", "goal": "g3"}}, "t4": {"task": {"id": "t4", "goal": "g4"}}}
GOALS = {"g3": GOAL, "g4": {"goal": {"id": "g4"}, "events": [{"position": 40, "type": "goal.approved", "refs": []}]}}
inp.hive = lambda key, kind, ident: TASKS[ident] if kind == "task" else GOALS[ident]
tdir = WORK / "t3"
tdir.mkdir()
(tdir / "KICKOFF.md").write_text("You are a worker.\n")


class A_:
    task, key, task_dir = "t3", "k", str(tdir)


out = __import__("io").StringIO()
sys.stdout, real = out, sys.stdout
rc = inp.cmd_stage(A_)
sys.stdout = real
r3 = json.loads(out.getvalue())
check(rc == 0 and [i["name"] for i in r3["inputs"]] == ["run-1"], "launcher: the approved inputs only (not the later revision's)")
check((tdir / "host-inputs").is_symlink() and (tdir / "host-inputs" / "run-1" / "adapter.safetensors").exists(),
      "launcher: host-inputs links to the copy")
kick = (tdir / "KICKOFF.md").read_text()
check("## Host inputs (read-only)" in kick and "host-inputs/run-1" in kick and "Kev's run" in kick, "launcher: a KICKOFF section")
rdir = WORK / "t3-review"
rdir.mkdir()
(rdir / "KICKOFF.md").write_text("You are a reviewer.\n")
A_.task_dir = str(rdir)
sys.stdout = __import__("io").StringIO()
inp.cmd_stage(A_)
sys.stdout = real
check(os.readlink(rdir / "host-inputs") == os.readlink(tdir / "host-inputs"), "launcher: the reviewer gets the worker's copy")
A_.task, A_.task_dir = "t4", str(WORK / "t4")
(WORK / "t4").mkdir()
(WORK / "t4" / "KICKOFF.md").write_text("x\n")
(WORK / "t4" / "host-inputs").symlink_to(dest)
sys.stdout = out = __import__("io").StringIO()
inp.cmd_stage(A_)
sys.stdout = real
check(json.loads(out.getvalue()) == {} and not os.path.lexists(WORK / "t4" / "host-inputs"),
      "launcher: a goal without inputs gets none, and a stale link goes")

# ---- the sweeper -------------------------------------------------------------------------------------------------
sw = load("sweeper")
now = dt.datetime.now(dt.timezone.utc)
old = (now - dt.timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
STATE = {"tasks": {}, "goals": {"g1": {"status": "accepted", "status_since": 50}, "g3": {"status": "active", "status_since": 31},
                                "g2": {"status": "abandoned", "status_since": 51}}}
EV = [{"position": 50, "recorded_at": old, "type": "goal.accepted"}, {"position": 51, "recorded_at": old, "type": "goal.abandoned"},
      {"position": 31, "recorded_at": old, "type": "goal.approved"}]
sw.hive = lambda *a: json.dumps(STATE) if a[0] == "state" else "\n".join(json.dumps(e) for e in EV)
sw.hubs = lambda: {}
sw.busy_paths = lambda: []
sw.agent_users = lambda: []
(WORK / ".inputs" / "g2" / "KEEP").write_text("")
sys.argv = ["sweeper.py"]
sys.stdout = __import__("io").StringIO()
sw.main()
sys.stdout = real
check(not (WORK / ".inputs" / "g1").exists(), "sweeper: an accepted goal's copy goes after the grace period")
check((WORK / ".inputs" / "g3").exists(), "sweeper: an active goal's copy stays")
check((WORK / ".inputs" / "g2").exists(), "sweeper: KEEP holds a copy")
check(any(json.loads(l)["folder"] == ".inputs/g1" for l in sw.LOG.read_text().splitlines()), "sweeper: the removal is logged")

subprocess.run(["chmod", "-R", "u+w", str(T)])
print(f"\n{len(fails)} failed" if fails else "\nall passed")
sys.exit(1 if fails else 0)
