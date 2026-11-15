#!/usr/bin/env python3
"""Host inputs: files on this host that a goal's agents may read, granted by the operator's approval of the goal.

    inputs.py stage <task> --key KEY --task-dir DIR   the launcher: stage the inputs of the task's goal as approved,
                                                      link DIR/host-inputs to them and add a section to DIR/KICKOFF.md;
                                                      prints the staging record as JSON, or {} when the goal has none
    inputs.py show <goal> [--key KEY]                 the goal's inputs: as approved (what its tasks get), and in its
                                                      current document if a revision changed them
    inputs.py check FILE                              a goal document's inputs block, parsed and checked on this host

Agents run in their own containers and see only their task folder, so a task that needed a file only this host has (a
model's weights, another tool's data) blocked with `needs: access` until the operator copied it in, and then its
reviewer, which can't see the worker's folder, blocked again (kg-1-options, 2026-11-14). Now the chief of staff
declares such files in the goal document, the operator's approval of the goal grants them, and every task of the goal,
and every review of those tasks, gets the same read-only copy.

The goal document (the `goal` ref of goal.proposed or goal.revised) declares them in one fenced block, with a path and
why the goal needs it on each line (at most 8):

    ```hive-inputs
    ~/opt/kev/runs/reader-ft-v1                               Kev's served run, for the CPU latency test
    ~/.cache/huggingface/hub/models--Qwen--Qwen3.5-4B-Base    the base model Kev loads
    ```

Rules:
- Only the document as approved counts: the last `goal` pin before goal.approved. A revision after approval grants
  nothing. A later need is a question to the operator, as before, or a new goal.
- A path is absolute or starts with `~/` (the operator's home), has no spaces, and resolves inside the allowed roots
  (HIVE_INPUT_ROOTS, `:`-separated, default the operator's home), not to a whole root. It may not be, contain or lie
  inside a place for keys and logins (DENY_HOME: ~/.ssh, ~/.config, ~/.claude, ...), the hive's config, or the work
  dir's own state (its dot folders).
- Copied, not mounted: once per goal, at the first launch after approval, to <work>/.inputs/<goal>/<digest>/, files
  0444 and folders 0555, every file's sha256 in MANIFEST.sha256. Every task of the goal and its reviews get that copy,
  read-only (`-v ...:ro` in a container), at the same path, linked from the task folder as `host-inputs`. So the
  reviewer reads what the worker read, even if the original changes. To take a newer original, the operator removes
  the copy; the next launch stages it again.
- Inside an input, a link to somewhere else in the same input stays a link (an HF cache's snapshots point at its
  blobs). A link outside it is copied as what it points at, if that path would be allowed, and left out otherwise.
  Files named like keys or tokens (SECRET_*) are left out. README.md in the copy lists everything left out.
- Limits: HIVE_INPUTS_MAX_BYTES per goal (default 64 GB), and 5 GB of the disk left free after the copy.
- The sweeper removes a goal's copy with its task folders: past the grace period after the goal is accepted or
  abandoned, unless <work>/.inputs/<goal>/KEEP exists.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import hive_paths as hp

HOME = Path(os.path.realpath(os.path.expanduser("~")))
BLOCK = re.compile(r"^```hive-inputs[ \t]*\n(.*?)^```[ \t]*$", re.M | re.S)
MAX_INPUTS, WHY_MAX = 8, 200
MAX_BYTES = int(float(os.environ.get("HIVE_INPUTS_MAX_BYTES") or 64e9))
RESERVE_BYTES = 5 * 10**9
DENY_HOME = [".ssh", ".gnupg", ".config", ".claude", ".claude.json", ".codex", ".aws", ".azure", ".kube", ".docker",
             ".netrc", ".git-credentials", ".password-store", ".pki", ".mozilla", ".local/share/keyrings",
             ".bash_history", ".cache/huggingface/token", ".cache/huggingface/stored_tokens"]
DENY_ABS = ["/etc", "/root", "/var/lib/1hive-agents", "/proc", "/sys", "/dev", "/run"]
SECRET_NAMES = {"token", "stored_tokens", ".env", ".netrc", ".git-credentials", "auth.json", "credentials",
                "credentials.json", "operator.key"}
SECRET_SUFFIXES = (".key", ".pem", ".p12", ".pfx")
SECRET_PREFIXES = ("id_rsa", "id_ed25519", "id_ecdsa", "id_dsa")


class Refused(Exception):
    """An input that can't be granted, or a goal whose inputs can't be read: the launch stops with this reason."""


# ---- the declaration ---------------------------------------------------------------------------------------------

def parse(text: str) -> list[tuple[str, str]]:
    """The (path, why) lines of a goal document's hive-inputs block; [] when it has none."""
    blocks = BLOCK.findall(text or "")
    if not blocks:
        if re.search(r"^```hive-inputs", text or "", re.M):
            raise Refused("the hive-inputs block isn't closed with a ``` line")
        return []
    if len(blocks) > 1:
        raise Refused("the goal document has more than one hive-inputs block")
    out = []
    for n, line in enumerate(blocks[0].splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        path, why = (re.split(r"\s+", line, maxsplit=1) + [""])[:2]
        why = why.strip()
        if not (path.startswith("/") or path.startswith("~/")):
            raise Refused(f"hive-inputs line {n}: {path}: a path starts with / or ~/")
        if not why:
            raise Refused(f"hive-inputs line {n}: {path}: say why the goal needs it, after the path")
        if len(why) > WHY_MAX:
            raise Refused(f"hive-inputs line {n}: the reason is over {WHY_MAX} characters")
        out.append((path.rstrip("/") or "/", why))
    if len(out) > MAX_INPUTS:
        raise Refused(f"hive-inputs: {len(out)} inputs, at most {MAX_INPUTS}")
    if len({p for p, _ in out}) != len(out):
        raise Refused("hive-inputs: a path is listed twice")
    return out


def expand(path: str) -> Path:
    return HOME / path[2:] if path.startswith("~/") else Path(path)


def roots() -> list[Path]:
    env = os.environ.get("HIVE_INPUT_ROOTS")
    return [Path(os.path.realpath(r)) for r in (env.split(":") if env else [str(HOME)]) if r]


def denied() -> list[Path]:
    d = [HOME / x for x in DENY_HOME] + [Path(x) for x in DENY_ABS] + [hp.CONFIG_DIR, hp.WORK_DIR / ".inputs"]
    try:
        d += [e for e in hp.WORK_DIR.iterdir() if e.name.startswith(".")]
    except OSError:
        pass
    return [Path(os.path.realpath(x)) for x in d]


def inside(p: Path, root: Path) -> bool:
    return p == root or root in p.parents


def not_allowed(real: Path) -> str | None:
    """Why a resolved path can't be granted, or None."""
    rs = roots()
    if not any(inside(real, r) for r in rs):
        return f"it is outside the allowed roots ({':'.join(map(str, rs))})"
    if real in rs:
        return "it is a whole allowed root: name the folder the goal needs"
    for d in denied():
        if inside(real, d):
            return f"it is inside {d}, a place for keys, logins or the hive's own state"
        if inside(d, real):
            return f"it contains {d}, a place for keys, logins or the hive's own state"
    return None


def resolve(path: str) -> Path:
    """The real path of a declared input, if it may be granted; Refused otherwise."""
    p = expand(path)
    real = Path(os.path.realpath(p))
    if not real.exists():
        raise Refused(f"{path}: no such file or folder on this host")
    why = not_allowed(real)
    if why:
        raise Refused(f"{path}: {why}")
    if not (real.is_dir() or real.is_file()):
        raise Refused(f"{path}: not a regular file or folder")
    return real


def secret_name(name: str) -> bool:
    return name in SECRET_NAMES or name.endswith(SECRET_SUFFIXES) or name.startswith(SECRET_PREFIXES)


# ---- the record --------------------------------------------------------------------------------------------------

def hive(key: str, *args: str) -> dict:
    r = subprocess.run(["hive", *args], env=hp.tool_env(key), capture_output=True, text=True)
    if r.returncode:
        raise Refused(f"hive {' '.join(args)}: {(r.stderr or r.stdout).strip()[-300:]}")
    return json.loads(r.stdout)


def approved(events: list[dict]) -> tuple[dict | None, int] | None:
    """The goal document pin as approved (the last `goal` ref before goal.approved) and the approval's position, or
    None if the goal was never approved."""
    pin = None
    for e in sorted(events, key=lambda e: e["position"]):
        if e["type"] in ("goal.proposed", "goal.revised"):
            for r in e.get("refs") or []:
                if r.get("rel") == "goal":
                    pin = r["pin"]
        elif e["type"] == "goal.approved":
            return pin, e["position"]
    return None


def read_pin(pin: dict) -> str:
    if (pin or {}).get("repository") != "workspace" or not pin.get("path"):
        raise Refused(f"the goal document isn't a workspace file: {pin}")
    oid = str(pin["commit_oid"]).split(":", 1)[-1]
    r = subprocess.run(["git", "-C", str(hp.WORKSPACE_REPO), "show", f"{oid}:{pin['path']}"],
                       capture_output=True, text=True)
    if r.returncode:
        raise Refused(f"can't read the goal document {pin['path']} at {oid[:8]}: {r.stderr.strip()[-200:]}")
    return r.stdout


# ---- staging -----------------------------------------------------------------------------------------------------

def plan(name: str, top: Path) -> tuple[list[tuple], list[tuple[str, str]]]:
    """What copying the input `top` (a real path) to `name` takes: ("dir", rel), ("file", src, rel, size) and
    ("link", rel, target) steps in order, and what is left out, as (rel, why)."""
    ops: list[tuple] = []
    skipped: list[tuple[str, str]] = []
    if top.is_file():
        if secret_name(top.name):
            raise Refused(f"{top}: named like a key or token")
        ops.append(("file", top, name, top.stat().st_size))
        return ops, skipped

    def walk(d: Path, rel: str, seen: frozenset, own: bool) -> None:
        if d in seen:
            skipped.append((rel, "a link loop"))
            return
        seen = seen | {d}
        ops.append(("dir", rel))
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError as e:
            skipped.append((rel, f"unreadable: {e.strerror}"))
            return
        for e in entries:
            r = f"{rel}/{e.name}"
            p = Path(e.path)
            if secret_name(e.name):
                skipped.append((r, "named like a key or token"))
                continue
            if e.is_symlink():
                t = Path(os.path.realpath(p))
                # Where the link itself points (one hop): inside the input, it stays a link to the copy of that,
                # even if that is a link out (an HF snapshot file -> the model's blob -> the hub's store).
                hop = Path(os.path.normpath(os.path.join(d, os.readlink(p))))
                if not t.exists():
                    skipped.append((r, "a link to nothing"))
                elif own and inside(hop, top):
                    ops.append(("link", r, os.path.relpath(hop, d)))
                elif why := not_allowed(t):
                    skipped.append((r, f"a link to {t}: {why}"))
                elif secret_name(t.name):
                    skipped.append((r, f"a link to {t}, named like a key or token"))
                elif t.is_dir():
                    walk(t, r, seen, False)
                elif t.is_file():
                    ops.append(("file", t, r, t.stat().st_size))
                else:
                    skipped.append((r, "not a regular file"))
            elif e.is_dir(follow_symlinks=False):
                walk(p, r, seen, own)
            elif e.is_file(follow_symlinks=False):
                ops.append(("file", p, r, e.stat(follow_symlinks=False).st_size))
            else:
                skipped.append((r, "not a regular file"))

    walk(top, name, frozenset(), True)
    return ops, skipped


def safe_name(path: str, taken: set) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]", "_", Path(path).name).lstrip(".") or "input"
    name, n = base, 2
    while name in taken or name in ("MANIFEST.sha256", "README.md", "STAGED.json"):
        name, n = f"{base}-{n}", n + 1
    taken.add(name)
    return name


def copy(src: Path, dst: Path) -> tuple[str, int]:
    h, n = hashlib.sha256(), 0
    with open(src, "rb") as f, open(dst, "xb") as g:
        while chunk := f.read(1 << 23):
            h.update(chunk)
            g.write(chunk)
            n += len(chunk)
    os.chmod(dst, 0o444)
    return h.hexdigest(), n


def make_writable(p: Path) -> None:
    """A staged copy is read-only, folders too: open its folders so it can be removed."""
    for d, _, _ in os.walk(p):
        os.chmod(d, 0o755)


def human(n: int) -> str:
    for unit, k in (("GB", 10**9), ("MB", 10**6), ("kB", 10**3)):
        if n >= k:
            return f"{n / k:.1f} {unit}"
    return f"{n} B"


def stage(goal: str, approved_at: int, pin: dict, inputs: list[tuple[str, str]]) -> dict:
    """The goal's staged copy, made once (under a lock, into a temporary folder renamed at the end)."""
    spec = {"goal": goal, "approved_at": approved_at, "goal_pin": pin, "inputs": [list(x) for x in inputs]}
    digest = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:12]
    gdir = hp.WORK_DIR / ".inputs" / goal
    dest = gdir / digest
    gdir.mkdir(parents=True, exist_ok=True)
    with open(gdir / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (dest / "STAGED.json").exists():
            return json.loads((dest / "STAGED.json").read_text())
        items, taken = [], set()
        for path, why in inputs:
            real = resolve(path)
            name = safe_name(path, taken)
            ops, skipped = plan(name, real)
            items.append({"name": name, "path": path, "real": str(real), "why": why, "ops": ops, "skipped": skipped})
        total = sum(op[3] for it in items for op in it["ops"] if op[0] == "file")
        if total > MAX_BYTES:
            raise Refused(f"the inputs come to {human(total)}, over the {human(MAX_BYTES)} limit (HIVE_INPUTS_MAX_BYTES)")
        free = shutil.disk_usage(gdir).free
        if free - total < RESERVE_BYTES:
            raise Refused(f"the inputs come to {human(total)} and the disk has {human(free)} free: "
                          f"{human(RESERVE_BYTES)} must stay free")
        for old in gdir.glob(".tmp-*"):   # a copy that stopped half-way (we hold the lock)
            make_writable(old)
            shutil.rmtree(old)
        tmp = gdir / f".tmp-{digest}-{os.getpid()}"
        tmp.mkdir()
        manifest = []
        try:
            for it in items:
                files = size = 0
                for op in it["ops"]:
                    if op[0] == "dir":
                        (tmp / op[1]).mkdir()
                    elif op[0] == "link":
                        os.symlink(op[2], tmp / op[1])
                    else:
                        h, n = copy(op[1], tmp / op[2])
                        manifest.append(f"{h}  {op[2]}")
                        files, size = files + 1, size + n
                it.update(files=files, bytes=size)
                del it["ops"]
        except OSError as e:
            make_writable(tmp)
            shutil.rmtree(tmp, ignore_errors=True)
            raise Refused(f"copying failed: {e}") from e
        now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        record = {"goal": goal, "approved_at": approved_at, "goal_pin": pin, "digest": digest, "staged_at": now,
                  "dir": str(dest), "inputs": items}
        (tmp / "MANIFEST.sha256").write_text("\n".join(manifest) + "\n")
        (tmp / "STAGED.json").write_text(json.dumps(record, indent=1) + "\n")
        (tmp / "README.md").write_text(readme(record))
        for d, dirs, files in os.walk(tmp, topdown=False):
            for f in files:
                if not os.path.islink(os.path.join(d, f)):
                    os.chmod(os.path.join(d, f), 0o444)
            os.chmod(d, 0o555)
        os.rename(tmp, dest)
        return json.loads(json.dumps(record))   # as a later launch reads it back


def readme(rec: dict) -> str:
    lines = [f"# Host inputs of goal {rec['goal']}", "",
             f"Read-only copies of host files the operator granted by approving the goal (record position "
             f"{rec['approved_at']}; goal document `{rec['goal_pin']['path']}` at "
             f"`{str(rec['goal_pin']['commit_oid']).split(':')[-1][:12]}`). Staged {rec['staged_at']} by the launcher "
             f"(hive-runtime inputs.py); every task of the goal and its reviews read this same copy. "
             f"`MANIFEST.sha256` has every file's sha256, relative to this folder (`sha256sum -c MANIFEST.sha256` here).", ""]
    for it in rec["inputs"]:
        lines.append(f"- `{it['name']}`: from `{it['path']}`: {it['why']} ({it['files']} files, {human(it['bytes'])})")
        for rel, why in it["skipped"]:
            lines.append(f"  - left out: `{rel}`: {why}")
    return "\n".join(lines) + "\n"


def kickoff_section(rec: dict, task_dir: Path) -> str:
    lines = ["", "## Host inputs (read-only)", "",
             f"By approving this goal (`{rec['goal']}`), the operator granted its agents these files from this host. "
             f"They are read-only copies, staged {rec['staged_at']}, the same for every task of the goal and its "
             f"reviews, in `{task_dir}/host-inputs/` (a link to `{rec['dir']}`):", ""]
    for it in rec["inputs"]:
        left = f"; {len(it['skipped'])} left out, see README.md there" if it["skipped"] else ""
        lines.append(f"- `host-inputs/{it['name']}`: from `{it['path']}`: {it['why']} "
                     f"({it['files']} files, {human(it['bytes'])}{left})")
    lines += ["", f"`MANIFEST.sha256` there has every file's sha256 (check them with `cd {rec['dir']} && sha256sum -c "
              "MANIFEST.sha256`). Cite these copies, not the host paths, which you can't reach. Never commit them to a "
              "repository.", ""]
    return "\n".join(lines)


def link(task_dir: Path, rec: dict | None) -> None:
    """task_dir/host-inputs: a link to the goal's copy, or nothing. A real file or folder of that name is left alone."""
    p = task_dir / "host-inputs"
    if p.is_symlink():
        p.unlink()
    if rec and not p.exists():
        p.symlink_to(rec["dir"])


# ---- commands ----------------------------------------------------------------------------------------------------

def goal_inputs(goal: str, key: str) -> tuple[dict, list[tuple[str, str]], dict | None, int | None]:
    g = hive(key, "goal", goal)
    a = approved(g.get("events") or [])
    if not a or not a[0]:
        return g, [], None, None
    pin, at = a
    return g, parse(read_pin(pin)), pin, at


def cmd_stage(a) -> int:
    t = hive(a.key, "task", a.task)
    goal = (t.get("task") or t).get("goal")
    task_dir = Path(a.task_dir) if a.task_dir else None
    rec = None
    if goal:
        _, inputs, pin, at = goal_inputs(goal, a.key)
        if inputs:
            rec = stage(goal, at, pin, inputs)
    if task_dir:
        link(task_dir, rec)
        if rec:
            with open(task_dir / "KICKOFF.md", "a") as f:
                f.write(kickoff_section(rec, task_dir))
    print(json.dumps(rec or {}))
    return 0


def describe(inputs: list[tuple[str, str]]) -> list[str]:
    out = []
    for path, why in inputs:
        try:
            real = resolve(path)
            ops, skipped = plan(Path(path).name, real)
            n = sum(op[3] for op in ops if op[0] == "file")
            files = sum(op[0] == "file" for op in ops)
            note = f"{files} files, {human(n)}" + (f", {len(skipped)} left out" if skipped else "")
            out.append(f"  ok      {path}  ({note}): {why}")
        except Refused as e:
            out.append(f"  REFUSED {e}")
    return out


def cmd_show(a) -> int:
    g, inputs, pin, at = goal_inputs(a.goal, a.key)
    if pin is None:
        print(f"{a.goal}: not approved: its tasks get nothing yet")
    elif not inputs:
        print(f"{a.goal}: approved at position {at}, with no host inputs")
    else:
        print(f"{a.goal}: approved at position {at}; its tasks get:")
        print("\n".join(describe(inputs)))
    cur = (g.get("goal") or {}).get("goal_pin")
    if cur and cur != pin:
        now = parse(read_pin(cur))
        if now != inputs:
            print(f"Its current document ({cur['path']}) declares{' none' if not now else ''}"
                  f"{':' if now else ''} (not granted: only the approved version counts)")
            print("\n".join(describe(now)))
    staged = sorted((hp.WORK_DIR / ".inputs" / a.goal).glob("*/STAGED.json"))
    for s in staged:
        r = json.loads(s.read_text())
        print(f"staged {r['staged_at']}: {r['dir']}")
    return 0


def cmd_check(a) -> int:
    inputs = parse(Path(a.file).read_text())
    if not inputs:
        print("no hive-inputs block")
        return 0
    lines = describe(inputs)
    print("\n".join(lines))
    return 1 if any("REFUSED" in x for x in lines) else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("stage")
    s.add_argument("task")
    s.add_argument("--key", required=True)
    s.add_argument("--task-dir")
    s.set_defaults(fn=cmd_stage)
    s = sub.add_parser("show")
    s.add_argument("goal")
    s.add_argument("--key", default=str(hp.CONFIG_DIR / "operator.key"))
    s.set_defaults(fn=cmd_show)
    s = sub.add_parser("check")
    s.add_argument("file")
    s.set_defaults(fn=cmd_check)
    a = ap.parse_args()
    try:
        return a.fn(a)
    except Refused as e:
        print(f"host inputs: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
