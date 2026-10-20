#!/usr/bin/env python3
"""A reviewer that runs a command per review: for a reviewer that is a model behind a CLI (Astra:
`openclaw agent exec --agent astra-reviewer …`, or `codex exec`), with its own record key.

    reviewer_runner.py --config <config.json>

The config is the Iter adapter's (adapters/iter/README.md: actor, hive_url, hive_id, key_file,
registry, workspace, repos), plus:
    "review_cmd":   the command, as a list; "{kickoff}" is replaced by the kickoff file's path,
                    "{text}" by its contents; it runs in the workspace clone
    "poll_seconds": default 30;  "timeout_seconds": default 5400;  "retry_seconds": default 900

It watches the record for results assigned to this reviewer that it hasn't judged, writes a kickoff
(the order, the result, each repository's base..code range), runs the command, and takes the verdict
from its output: the last line that reads `VERDICT: passed|failed|needs_information`. The output is
the review report, committed to the workspace and recorded as review.recorded, citing the result (A5).
A run that gives no verdict is retried later; nothing is recorded for it.
"""
import importlib.util
import json
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHANNEL = HERE.parent / "iter" / "channels" / "hive.py"
VERDICT = re.compile(r"^\s*VERDICT:\s*(passed|failed|needs_information)\s*$", re.I | re.M)


def load_channel():
    spec = importlib.util.spec_from_file_location("hive_channel", CHANNEL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def kickoff(h, t):
    """What the reviewer gets: the order, the result, and where the code is."""
    cr = t["current_result"]
    lines = [f"# Review {t['id']}: {t.get('title', '')}", "",
             f"You are {h.actor}, an independent reviewer. Judge the result below against its order. Re-run what "
             "matters; don't trust the report's claims, and don't fix anything. Write your findings with evidence, "
             "then end with one line: VERDICT: passed, VERDICT: failed, or VERDICT: needs_information.", "",
             "## Order", h.read_pin(t.get("order")), "", f"## Result (by {cr.get('author')})", h.read_pin(cr.get("pin"))]
    refs = {}
    try:
        evs = json.loads(h.hive("task", t["id"])).get("events") or []
        res = next((e for e in reversed(evs) if e.get("event_id") == cr.get("event_id")), None)
        for r in (res or {}).get("refs") or []:
            if r["rel"] in ("code", "base"):
                refs.setdefault(r["pin"]["repository"], {})[r["rel"]] = r["pin"]["commit_oid"].split(":")[-1]
    except (ValueError, KeyError):
        pass
    for repo, cb in refs.items():
        clone = h.repos.get(repo)
        lines += ["", f"## Code: {repo}", f"base {cb.get('base')} .. code {cb.get('code')}"
                  + (f"; your clone: {clone} (run `git fetch` there, then `git diff {cb.get('base')}..{cb.get('code')}` "
                     f"and check out {cb.get('code')} to run things)" if clone else " (no clone configured)")]
    return "\n".join(lines) + "\n"


def main():
    args = sys.argv[1:]
    if "--config" not in args:
        sys.exit(__doc__)
    conf = Path(args[args.index("--config") + 1])
    mod = load_channel()
    c = json.loads(conf.read_text())
    mod.configure(c.get("root") or conf.parent, conf)
    mod._dirs()
    h = mod.Hive(c)
    every, timeout, retry = int(c.get("poll_seconds", 30)), int(c.get("timeout_seconds", 5400)), int(c.get("retry_seconds", 900))
    tried = {}   # result event -> when last tried without a verdict
    mod.log(f"reviewer runner up as {h.actor}")
    while True:
        try:
            st = h.state()
            for t in st["tasks"].values():
                cr = t.get("current_result") or {}
                lr = t.get("latest_review") or {}
                if (t.get("assigned_reviewer") != h.actor or t["status"] != "in_review" or not cr
                        or lr.get("reviewer") == h.actor or time.time() - tried.get(cr["event_id"], 0) < retry):
                    continue
                tried[cr["event_id"]] = time.time()
                kf = mod.RUN / "kickoffs" / f"{t['id']}-{cr['event_id'][:8]}.md"
                kf.parent.mkdir(parents=True, exist_ok=True)
                kf.write_text(kickoff(h, t))
                cmd = [kf.read_text() if a == "{text}" else str(kf) if a == "{kickoff}" else a for a in c["review_cmd"]]
                mod.log(f"reviewing {t['id']} ({cr['event_id'][:8]})")
                try:
                    r = subprocess.run(cmd, cwd=h.ws["clone"], capture_output=True, text=True, timeout=timeout)
                    out = (r.stdout or "") + (("\n" + r.stderr) if r.returncode else "")
                except subprocess.TimeoutExpired:
                    mod.log(f"review of {t['id']} timed out after {timeout}s")
                    continue
                m = VERDICT.findall(out)
                if not m:
                    mod.log(f"review of {t['id']}: no verdict in the output (rc {r.returncode}); retry in {retry}s")
                    continue
                verdict = m[-1].lower()
                try:
                    mod.log("recorded: " + mod.do(h, {"action": "review", "task": t["id"], "text": out.strip(), "extra": verdict}))
                except mod.Failed as e:
                    mod.log(f"recording the review of {t['id']} failed: {e}")
        except Exception as e:   # keep running through record or network errors
            mod.log(f"poll failed: {type(e).__name__}: {e}")
        time.sleep(every)


if __name__ == "__main__":
    main()
