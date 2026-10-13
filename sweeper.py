#!/usr/bin/env python3
"""1-hive sweeper: removes task folders once the record says they're no longer needed.

    sweeper.py [--dry-run] [--grace-days 3]

Run daily by the 1-hive-sweeper timer (deploy/up.sh). Asked for by cos
(workspace hive-requests/2026-10-13-task-folder-cleanup.md). A folder ~/work/1hive/<task>
or <task>-review is removed when all of these hold:
1. its task is terminal (done or cancelled);
2. its goal is settled (accepted or abandoned): a goal awaiting acceptance keeps its folders;
3. the grace period has passed since the goal settled (the operator often plays or checks
   from a task folder after accepting);
4. nothing is lost: every commit the task's results pin is in its repository's hub;
5. there is no KEEP file at its top (the operator or cos can drop one to hold a folder);
6. nothing running uses it: no process of ours with its working directory inside, and no
   container of an agent user mounting it.
Folders that aren't task folders (cos, canary, play, dot-folders) are never touched.

Agent users own parts of their folders, some through their containers' sub-ids, so what the
operator's user can't remove is removed by each owner in its own `podman unshare` (the sudo
rule the launcher uses). Afterwards each agent user's stopped containers and unused images
older than the grace period are pruned, and the operator's dangling images.

Each removal is a line in ~/work/1hive/.sweeper/log.jsonl (folder, task, goal, bytes, time);
the Telegram digest sums the ones since the last digest. Reads the record as `floor`, which
never emits.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pwd
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path.home() / "work" / "1hive"
LOG = ROOT / ".sweeper" / "log.jsonl"
REGISTRY = Path(__file__).resolve().parents[1] / "deploy" / "registry.json"
AGENT_PODMAN = "/usr/local/bin/hive-agent-podman"
ENV = {**os.environ, "HIVE_URL": "http://127.0.0.1:8470", "HIVE_ID": "1-hive",
       "HIVE_KEY_FILE": str(Path.home() / ".config/hive/floor.key"),
       "PATH": f"{Path.home()}/.local/bin:{os.environ.get('PATH', '')}"}
NOT_TASKS = {"cos", "canary", "play"}


def log(msg: str) -> None:
    print(f"{dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%M:%SZ} {msg}", flush=True)


def hive(*args: str) -> str:
    return subprocess.run(["hive", *args], env=ENV, capture_output=True, text=True, check=True).stdout


def ts(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def hubs() -> dict[str, Path]:
    """Each registered repository's hub: the local remote its checkout pushes to, or itself."""
    out = {}
    for name, r in json.loads(REGISTRY.read_text())["repositories"].items():
        p = Path(os.path.expanduser(r["local_path"]))
        url = subprocess.run(["git", "-C", str(p), "remote", "get-url", "origin"],
                             capture_output=True, text=True).stdout.strip()
        out[name] = Path(url) if url.startswith("/") else p
    return out


def agent_users() -> list[str]:
    return [u.pw_name for u in pwd.getpwall() if u.pw_name.startswith("hive-") and u.pw_name != "hive-cos"]


def busy_paths() -> list[str]:
    """Working directories of our processes, and mounts of agent users' running containers."""
    paths = []
    for p in Path("/proc").iterdir():
        if p.name.isdigit():
            try:
                paths.append(os.readlink(p / "cwd"))
            except OSError:
                pass
    for u in agent_users():
        r = subprocess.run(["sudo", "-n", "-u", u, AGENT_PODMAN, "ps", "--format", "{{.Mounts}}"],
                           capture_output=True, text=True)
        paths += r.stdout.replace("[", " ").replace("]", " ").split()
    return paths


def size(p: Path) -> int:
    r = subprocess.run(["du", "-sb", str(p)], capture_output=True, text=True)
    return int(r.stdout.split()[0]) if r.stdout else 0


def remove(p: Path) -> bool:
    shutil.rmtree(p, ignore_errors=True)
    if p.exists():  # what agent users own, each in its own user namespace
        owners = subprocess.run(["find", str(p), "!", "-user", pwd.getpwuid(os.getuid()).pw_name,
                                 "-printf", "%u\n"], capture_output=True, text=True).stdout.split()
        for u in sorted({o for o in owners if o.startswith("hive-")}):
            subprocess.run(["sudo", "-n", "-u", u, AGENT_PODMAN, "unshare", "rm", "-rf", str(p)],
                           capture_output=True)
        shutil.rmtree(p, ignore_errors=True)
    return not p.exists()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="list what would go; remove nothing")
    ap.add_argument("--grace-days", type=float, default=3)
    a = ap.parse_args()
    now = dt.datetime.now(dt.timezone.utc)
    state = json.loads(hive("state"))
    state = state.get("state", state)
    tasks, goals = state["tasks"], state["goals"]
    at = {}  # position -> recorded_at, for when each goal settled
    results: dict[str, list[dict]] = {}
    for line in hive("events").splitlines():
        e = json.loads(line)
        at[e["position"]] = e["recorded_at"]
        if e["type"] == "task.result_posted":
            results.setdefault(e.get("task"), []).extend(e.get("refs") or [])
    hub = hubs()
    busy = busy_paths()
    freed, removed = 0, 0
    for d in sorted(ROOT.iterdir()):
        if not d.is_dir() or d.name.startswith(".") or d.name in NOT_TASKS:
            continue
        tid = d.name.removesuffix("-review")
        t = tasks.get(tid)
        why = None
        g = goals.get((t or {}).get("goal"), {})
        if not t:
            why = "no such task on the record"
        elif t["status"] not in ("done", "cancelled"):
            why = f"task {t['status']}"
        elif g.get("status") not in ("accepted", "abandoned"):
            why = f"goal {g.get('status')}"
        elif now - ts(at[g["status_since"]]) < dt.timedelta(days=a.grace_days):
            why = f"goal {g['status']} {ts(at[g['status_since']]):%Y-%m-%d}, in the grace period"
        elif (d / "KEEP").exists():
            why = "KEEP"
        elif any(b == str(d) or b.startswith(str(d) + "/") for b in busy):
            why = "in use"
        else:
            for r in results.get(tid, []):
                pin = r["pin"]
                repo = hub.get(pin["repository"])
                oid = pin["commit_oid"].split(":", 1)[1]
                if not repo or subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{oid}^{{commit}}"],
                                              capture_output=True).returncode:
                    why = f"{pin['repository']} {oid[:8]} isn't in its hub: kept, check it"
                    break
        if why:
            if a.dry_run:
                print(f"keep   {d.name}: {why}")
            continue
        n = size(d)
        if a.dry_run:
            print(f"remove {d.name}: {n / 1e9:.1f} GB")
            continue
        if remove(d):
            freed, removed = freed + n, removed + 1
            LOG.parent.mkdir(exist_ok=True)
            with LOG.open("a") as f:
                f.write(json.dumps({"at": f"{now:%Y-%m-%dT%H:%M:%SZ}", "folder": d.name, "task": tid,
                                    "goal": t["goal"], "bytes": n}) + "\n")
        else:
            log(f"{d.name}: couldn't remove everything; left for the next sweep")
    if a.dry_run:
        return
    hours = f"{int(a.grace_days * 24)}h"
    for u in agent_users():
        for cmd in (["container", "prune", "-f", "--filter", f"until={hours}"],
                    ["image", "prune", "-a", "-f", "--filter", f"until={hours}"]):
            subprocess.run(["sudo", "-n", "-u", u, AGENT_PODMAN, *cmd], capture_output=True)
    subprocess.run(["podman", "image", "prune", "-f"], capture_output=True)
    log(f"removed {removed} task folders, {freed / 1e9:.1f} GB")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        sys.exit(f"sweeper: {' '.join(e.cmd)}: {e.stderr.strip()[-300:]}")
