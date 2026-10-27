"""End to end: two Iter agents (a worker and a reviewer) on a scratch hive record, through the
adapters/iter channel and tool, called the way iter.py calls them (a fresh process per call, from
the agent's folder). Needs rootless Podman and a hive-record checkout:

    uv run --project ~/repos/hive-record python tests/iter_adapter_e2e.py

The task goes: assigned -> accepted -> progress (also through send) -> blocked -> answered ->
unblocked -> result with code and base pins -> review failed -> new result -> review passed -> closed.
"""
from __future__ import annotations

import json
import re
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
from hiverecord import scratch
from hiverecord.canonical import dumps
from hiverecord.conformance import FixtureRepo

ADAPTER = Path(__file__).resolve().parents[1] / "adapters" / "iter"
HIVE = [sys.executable, "-m", "hiverecord.cli"]
HIVE_PIN = [sys.executable, "-m", "hivepin.cli"]


def sh(*cmd, cwd=None, env=None):
    r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"FAILED: {' '.join(map(str, cmd))}\n{r.stdout}{r.stderr}")
    return r.stdout


def invoke(agent: Path, path: Path, function: str, *args):
    """As iter.py's invoke_dynamic: a new process, the agent's folder as cwd, 5 s limit."""
    code = ("import importlib.util, json, sys\n"
            "spec = importlib.util.spec_from_file_location('m', sys.argv[1]); m = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(m); r = getattr(m, sys.argv[2])(*json.loads(sys.argv[3]))\n"
            "print(json.dumps(r))")
    r = subprocess.run([sys.executable, "-c", code, str(path), function, json.dumps(args)], cwd=agent,
                       capture_output=True, text=True, timeout=5)
    if r.returncode:
        raise SystemExit(f"FAILED: {function} in {path.name}: {r.stderr[-800:]}")
    return json.loads(r.stdout)


class Agent:
    def __init__(self, root: Path, actor: str, conf: dict, sidecar: bool = False):
        """sidecar=True: the agent's folder holds only {"daemon": "sidecar"}; the real config lives outside
        it and the daemon is sidecar.py, started here as a separate process (on vm2: a systemd unit)."""
        self.root, self.actor, self.proc = root, actor, None
        (root / "channels").mkdir(parents=True)
        (root / "tools").mkdir()
        (root / ".hive").mkdir()
        shutil.copy(ADAPTER / "channels" / "hive.py", root / "channels" / "hive.py")
        shutil.copy(ADAPTER / "tools" / "hive_task.py", root / "tools" / "hive_task.py")
        if sidecar:
            (root / ".hive" / "config.json").write_text(json.dumps({"daemon": "sidecar"}))
            outside = root.parent / f"{actor}-sidecar-config.json"
            outside.write_text(json.dumps(conf, indent=1))
            self.proc = subprocess.Popen([sys.executable, str(ADAPTER / "sidecar.py"), "--root", str(root),
                                          "--config", str(outside)], stdin=subprocess.DEVNULL)
        else:
            (root / ".hive" / "config.json").write_text(json.dumps(conf, indent=1))
        self.heard: list[str] = []

    def receive(self):
        got = invoke(self.root, self.root / "channels" / "hive.py", "receive")
        if got:
            self.heard.append(got)
        return got

    def tool(self, *args):
        return invoke(self.root, self.root / "tools" / "hive_task.py", "run", *args)

    def send(self, text):
        return invoke(self.root, self.root / "channels" / "hive.py", "send", text)

    def wait_for(self, needle, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            got = self.receive()
            if got and needle in got:
                print(f"  {self.actor} heard: {got.splitlines()[0][:110]}")
                return got
            if got and got.startswith("FAILED"):
                raise SystemExit(f"{self.actor}: {got}")
            time.sleep(1)
        raise SystemExit(f"{self.actor} never heard {needle!r}; heard: {self.heard[-3:]}")

    def stop(self):
        if self.proc:
            self.proc.terminate()
            return
        try:
            os.kill(int((self.root / ".runtime" / "hive" / "pid").read_text()), 15)
        except Exception:
            pass


BEACONS: list[dict] = []


class Beacons(BaseHTTPRequestHandler):
    """Stands in for the Floor: keeps each beacon's body."""
    def do_POST(self):
        BEACONS.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *a):
        pass


def main():
    tmp = Path(tempfile.mkdtemp(prefix="iter-e2e-"))
    floor = ThreadingHTTPServer(("127.0.0.1", 0), Beacons)
    threading.Thread(target=floor.serve_forever, daemon=True).start()
    beacon = {"url": f"http://127.0.0.1:{floor.server_address[1]}/api/beacon", "every": 1}
    fx = FixtureRepo()
    pg = scratch.up()
    agents: list[Agent] = []
    gw = None
    try:
        # git: a workspace and a code repository, each a bare remote plus clones
        gitenv = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                  "GIT_COMMITTER_EMAIL": "t@t"}
        for name in ("workspace", "code"):
            sh("git", "init", "-q", "--bare", "-b", "main", str(tmp / f"{name}.git"))
            seed = tmp / f"seed-{name}"
            sh("git", "clone", "-q", str(tmp / f"{name}.git"), str(seed), env=gitenv)
            (seed / "README.md").write_text(f"{name}\n")
            sh("git", "-C", str(seed), "add", "-A", env=gitenv)
            sh("git", "-C", str(seed), "commit", "-q", "-m", "start", env=gitenv)
            sh("git", "-C", str(seed), "push", "-q", "origin", "HEAD:main", env=gitenv)
        reg = json.loads(fx.registry_path.read_text())
        for name in ("workspace", "code"):
            reg["repositories"][name] = {"fetch_urls": [f"file://{tmp}/{name}.git"], "object_format": "sha1",
                                         "allowed_ref_patterns": ["refs/heads/*"], "local_path": str(tmp / f"seed-{name}")}
        registry = tmp / "registry.json"
        registry.write_text(json.dumps(reg, indent=1))
        env = {**os.environ, "HIVEPIN_CACHE_DIRECTORY": str(tmp / "pincache"), "HIVEPIN_REPOSITORY_REGISTRY_PATH": str(registry)}

        # the record: roles, keys, a 1-hive-profile hive, a gateway
        secrets = tmp / "secrets.json"
        sh(*HIVE, "db-bootstrap", "--superuser-url", pg.superuser_url, "--db", "iterhive", "--prefix", "it",
           "--secrets-file", str(secrets))
        pw = json.loads(secrets.read_text())
        url = {r: pg.url(f"it_{r}", pw[f"it_{r}"], "iterhive") for r in ("admin", "gateway")}
        pub = {a: sh(*HIVE, "keygen", "--out", str(tmp / f"{a}.key")).strip() for a in ("op", "cos", "sup", "w1", "rv1")}
        (tmp / "policy.pin").write_bytes(dumps(fx.policy_pin()))
        sh(*HIVE, "init", "--admin-url", url["admin"], "--prefix", "it", "--hive", "iterhive", "--profile", "1-hive",
           "--policy", str(tmp / "policy.pin"), "--mode", "authoritative", "--registry", str(registry),
           "--operator-id", "op", "--operator-key", pub["op"], env=env)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        gw = subprocess.Popen([*HIVE, "gateway", "--db-url", url["gateway"], "--registry", str(registry), "--port", str(port)],
                              env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                if httpx.get(base + "/v1/health").status_code == 200:
                    break
            except httpx.TransportError:
                time.sleep(0.1)

        def hive(actor, *args):
            return sh(*HIVE, *args, env={**env, "HIVE_URL": base, "HIVE_ID": "iterhive", "HIVE_KEY_FILE": str(tmp / f"{actor}.key")})

        def pin_file(repo, clone, path):
            out = tmp / f"pin-{time.time_ns()}.json"
            sh(*HIVE_PIN, "mint", repo, path, "--commit", sh("git", "-C", str(clone), "rev-parse", "HEAD").strip(),
               "--output", str(out), env=env)
            return str(out)

        def commit_ws(path, text, msg):
            ws = tmp / "seed-workspace"
            sh("git", "-C", str(ws), "pull", "-q", "--ff-only", env=gitenv)
            (ws / path).parent.mkdir(parents=True, exist_ok=True)
            (ws / path).write_text(text)
            sh("git", "-C", str(ws), "add", "-A", env=gitenv)
            sh("git", "-C", str(ws), "commit", "-q", "-m", msg, env=gitenv)
            sh("git", "-C", str(ws), "push", "-q", "origin", "HEAD:main", env=gitenv)
            return pin_file("workspace", ws, path)

        for a, cls in (("cos", "chief_of_staff"), ("sup", "supervisor"), ("w1", "worker"), ("rv1", "reviewer")):
            hive("op", "emit", "actor.registered", "--data", json.dumps(
                {"actor_id": a, "class": cls, "role": cls, "keys": [pub[a]],
                 "declaration": {"harness": "iter", "model_route": "none", "sandbox": a}}))
        hive("cos", "emit", "project.opened", "--data", '{"project":"p1","title":"P1"}')
        gpin = commit_ws("projects/p1/goals/g1.md", "# goal\n", "goal")
        hive("cos", "emit", "goal.proposed", "--goal", "g1", "--ref", f"goal={gpin}", "--data", json.dumps(
            {"project": "p1", "title": "G1", "objective": "O", "relevance": "R", "budget": {"tokens": 10}}))
        hive("op", "emit", "goal.approved", "--goal", "g1")
        opin = commit_ws("projects/p1/orders/t1.md", "# Order t1\n\nAdd a line to code/README.md.\n", "order")
        hive("cos", "emit", "task.created", "--task", "t1", "--goal", "g1", "--ref", f"order={opin}",
             "--data", json.dumps({"title": "Add a line", "project": "p1", "ext": {"kind": "work", "repos": ["code"]}}))
        hive("cos", "emit", "task.assigned", "--task", "t1", "--data", json.dumps(
            {"to": "w1", "ext": {"lease": {"accept_within_seconds": 600, "checkin_every_seconds": 900}}}))

        # two Iter agents, each with its own key, workspace clone and (the worker) a code clone
        def conf(actor, repos):
            ws = tmp / f"{actor}-workspace"
            sh("git", "clone", "-q", str(tmp / "workspace.git"), str(ws), env=gitenv)
            return {"actor": actor, "hive_url": base, "hive_id": "iterhive", "key_file": str(tmp / f"{actor}.key"),
                    "registry": str(registry), "hive_bin": str(Path(sys.executable).parent / "hive"),
                    "hive_pin_bin": str(Path(sys.executable).parent / "hive-pin"),
                    "workspace": {"repository": "workspace", "clone": str(ws), "branch": "main"},
                    "repos": repos, "poll_seconds": 2}
        code = tmp / "w1-code"
        sh("git", "clone", "-q", str(tmp / "code.git"), str(code), env=gitenv)
        w1 = Agent(tmp / "iter-w1", "w1", {**conf("w1", {"code": str(code)}), "declare_seconds": 2, "beacon": beacon,
                                          "declare_files": {"flags": "memory/flags.json"}, "propose_goal": "g1"}, sidecar=True)   # as on vm2
        rv1 = Agent(tmp / "iter-rv1", "rv1", conf("rv1", {}))
        agents += [w1, rv1]
        os.environ["HIVEPIN_CACHE_DIRECTORY"] = str(tmp / "pincache")

        print("worker:")
        w1.wait_for("New task t1")
        assert "Add a line to code/README.md" in w1.heard[-1], w1.heard[-1]
        print("  ", w1.tool("accept", "t1", "", ""))
        w1.wait_for("OK: accepted t1")
        w1.tool("progress", "t1", "Plan: add the line, test, post.", "")
        w1.wait_for("OK: progress report on t1")
        hive("sup", "emit", "task.nudged", "--task", "t1", "--data", json.dumps({"reason": "no check-in for 40 min"}))
        w1.wait_for("Nudge on t1 from the supervisor")
        assert "no check-in for 40 min" in w1.heard[-1], w1.heard[-1]
        w1.send("Started; nothing surprising.")
        w1.wait_for("OK: progress report on t1")
        w1.tool("block", "t1", "Which line should I add?\nOptions: a or b. Recommend a.", "decision")
        w1.wait_for("OK: t1 is blocked")
        apin = commit_ws("projects/p1/questions/t1-answer.md", "Add line a.\n", "answer")
        hive("cos", "emit", "task.answered", "--task", "t1", "--ref", f"answer={apin}")
        w1.wait_for("was answered")
        assert "Add line a." in w1.heard[-1], w1.heard[-1]
        # a second answer on the same block (the chief of staff adds to it) reaches the agent too
        apin2 = commit_ws("projects/p1/questions/t1-answer-2.md", "And keep it short.\n", "answer 2")
        hive("cos", "emit", "task.answered", "--task", "t1", "--ref", f"answer={apin2}")
        w1.wait_for("keep it short")
        w1.tool("unblock", "t1", "", "")
        w1.wait_for("OK: t1 unblocked")
        # the result is refused until the code is pushed
        (code / "README.md").write_text("code\na\n")
        sh("git", "-C", str(code), "commit", "-q", "-am", "add a", env=gitenv)
        w1.tool("result", "t1", "Added line a.", "")
        got = ""
        end = time.time() + 30
        while "push code first" not in got and time.time() < end:
            got = invoke(w1.root, w1.root / "channels" / "hive.py", "receive") or ""
            time.sleep(1)
        assert "push code first" in got, got
        print("  w1 heard: FAILED (unpushed code), as it should")
        sh("git", "-C", str(code), "push", "-q", "origin", "HEAD:hive/t1", env=gitenv)
        w1.tool("result", "t1", "Added line a.", "")
        w1.wait_for("OK: result posted on t1")
        st = json.loads(hive("cos", "task", "t1"))
        cr = st["task"]["current_result"]

        print("reviewer:")
        hive("cos", "emit", "review.assigned", "--task", "t1", "--data", json.dumps({"reviewer": "rv1", "result_event": cr["event_id"]}))
        rv1.wait_for("Review t1")
        assert "Added line a." in rv1.heard[-1], rv1.heard[-1]
        rv1.tool("review", "t1", "The line has no test.", "failed")
        rv1.wait_for("OK: review of t1: failed")
        w1.wait_for("Review of your result on t1: failed")
        assert "no test" in w1.heard[-1]
        (code / "test.sh").write_text("grep -qx a README.md\n")
        sh("git", "-C", str(code), "add", "-A", env=gitenv)
        sh("git", "-C", str(code), "commit", "-q", "-m", "test", env=gitenv)
        sh("git", "-C", str(code), "push", "-q", "origin", "HEAD:hive/t1", env=gitenv)
        w1.tool("result", "t1", "Added line a, with a test.", "")
        w1.wait_for("OK: result posted on t1")
        cr = json.loads(hive("cos", "task", "t1"))["task"]["current_result"]
        hive("cos", "emit", "review.assigned", "--task", "t1", "--data", json.dumps({"reviewer": "rv1", "result_event": cr["event_id"]}))
        rv1.wait_for("Review t1")
        rv1.tool("review", "t1", "Line and test are there.", "passed")
        rv1.wait_for("OK: review of t1: passed")
        hive("cos", "emit", "task.closed", "--task", "t1")
        w1.wait_for("Task t1 (Add a line) is closed after a passed review")
        t = json.loads(hive("cos", "task", "t1"))["task"]
        assert t["status"] == "done", t["status"]
        types = [json.loads(l)["type"] for l in hive("cos", "events").splitlines() if l.strip()]
        need = ["task.accepted", "task.reported", "task.blocked", "task.answered", "task.unblocked",
                "task.result_posted", "review.recorded", "task.closed"]
        assert all(x in types for x in need), types
        refs = [json.loads(l) for l in hive("cos", "events").splitlines() if '"task.result_posted"' in l][-1]["refs"]
        assert sorted(r["rel"] for r in refs) == ["base", "code", "result"], refs

        print("hive-task (OpenClaw) + reviewer runner:")
        # t2: w1 works it through the synchronous hive-task CLI (an agent with a shell, no daemon needed);
        # rv1 reviews it through the reviewer runner, with a stand-in review command.
        opin2 = commit_ws("projects/p1/orders/t2.md", "# Order t2\n\nAdd a second line.\n", "order 2")
        hive("cos", "emit", "task.created", "--task", "t2", "--goal", "g1", "--ref", f"order={opin2}",
             "--data", json.dumps({"title": "Second line", "project": "p1", "ext": {"kind": "work", "repos": ["code"]}}))
        hive("cos", "emit", "task.assigned", "--task", "t2", "--data", json.dumps(
            {"to": "w1", "ext": {"lease": {"accept_within_seconds": 600, "checkin_every_seconds": 900}}}))
        henv = {**os.environ, "HIVE_TASK_CONFIG": str(tmp / "w1-sidecar-config.json")}
        def ht(*a):
            r = subprocess.run([sys.executable, str(ADAPTER.parent / "openclaw" / "hive-task"), *a], env=henv,
                               capture_output=True, text=True, timeout=120)
            return (r.stdout + r.stderr).strip()
        for _ in range(20):
            got = ht("inbox")
            if "New task t2" in got:
                break
            time.sleep(2)
        assert "New task t2" in got, got
        print("  hive-task inbox:", got.splitlines()[0][:80])
        assert ht("accept", "t2").startswith("OK: accepted t2"), ht("accept", "t2")
        assert ht("progress", "t2", "Adding", "line", "b").startswith("OK: progress"), "progress"
        (code / "README.md").write_text("code\na\nb\n")
        sh("git", "-C", str(code), "commit", "-q", "-am", "add b", env=gitenv)
        assert "push code first" in ht("result", "t2", "Added line b."), "unpushed result must be refused"
        sh("git", "-C", str(code), "push", "-q", "origin", "HEAD:hive/t2", env=gitenv)
        assert ht("result", "t2", "Added line b.").startswith("OK: result posted"), "result"
        print("  hive-task: accept, progress, result refused until pushed, result posted")
        # t3: hive-task in sidecar mode: it only queues, w1's sidecar holds the key and does the work
        (tmp / "w1-queue-config.json").write_text(json.dumps({"daemon": "sidecar", "root": str(w1.root), "wait_seconds": 60}))
        qenv = {**os.environ, "HIVE_TASK_CONFIG": str(tmp / "w1-queue-config.json")}
        def hq(*a):
            r = subprocess.run([sys.executable, str(ADAPTER.parent / "openclaw" / "hive-task"), *a], env=qenv,
                               capture_output=True, text=True, timeout=120)
            return (r.stdout + r.stderr).strip()
        opin3 = commit_ws("projects/p1/orders/t3.md", "# Order t3\n\nA third look.\n", "order 3")
        hive("cos", "emit", "task.created", "--task", "t3", "--goal", "g1", "--ref", f"order={opin3}",
             "--data", json.dumps({"title": "Third", "project": "p1", "ext": {"kind": "work"}}))
        hive("cos", "emit", "task.assigned", "--task", "t3", "--data", json.dumps(
            {"to": "w1", "ext": {"lease": {"accept_within_seconds": 600, "checkin_every_seconds": 900}}}))
        for _ in range(20):
            got = hq("inbox")
            if "New task t3" in got:
                break
            time.sleep(2)
        assert "New task t3" in got, got
        out = hq("accept", "t3")
        assert "OK: accepted t3" in out, out
        assert "FAILED:" in hq("review", "t3", "passed", "x"), "a worker can't review"
        assert json.loads(hive("cos", "task", "t3"))["task"]["status"] == "in_progress"
        print("  hive-task in sidecar mode: inbox from the sidecar, accept queued and answered, a refusal reported")
        fake = tmp / "fake-review.py"
        fake.write_text("import sys, pathlib; k = open(sys.argv[1]).read()\nprint('Reviewed:', k.splitlines()[0])\n"
                        f"'Set up a venv with uv.' in k and pathlib.Path({str(tmp / 'notes-seen')!r}).write_text('y')\n"
                        "print('base..code named:', 'base ' in k)\nprint('VERDICT: passed')\n")
        rconf = {**conf("rv1-runner-" + "x", {}), "actor": "rv1", "key_file": str(tmp / "rv1.key"),
                 "root": str(tmp / "rv1-runner"), "review_cmd": [sys.executable, str(fake), "{kickoff}"],
                 "review_notes": "Set up a venv with uv.",
                 "poll_seconds": 2, "beacon": beacon}
        (tmp / "rv1-runner").mkdir()
        (tmp / "rv1-runner-config.json").write_text(json.dumps(rconf))
        runner = subprocess.Popen([sys.executable, str(ADAPTER.parent / "reviewer" / "reviewer_runner.py"),
                                   "--config", str(tmp / "rv1-runner-config.json")], stdin=subprocess.DEVNULL)
        agents.append(type("P", (), {"stop": lambda self, p=runner: p.terminate()})())
        cr2 = json.loads(hive("cos", "task", "t2"))["task"]["current_result"]
        hive("cos", "emit", "review.assigned", "--task", "t2", "--data", json.dumps({"reviewer": "rv1", "result_event": cr2["event_id"]}))
        for _ in range(45):
            t2 = json.loads(hive("cos", "task", "t2"))["task"]
            if (t2.get("latest_review") or {}).get("reviewer") == "rv1":
                break
            time.sleep(2)
        assert t2["latest_review"]["verdict"] == "passed", t2.get("latest_review")
        rev = [json.loads(l) for l in hive("cos", "events").splitlines() if '"review.recorded"' in l and '"t2"' in l][-1]
        assert rev.get("cause") == {"event": cr2["event_id"]}, rev.get("cause")
        assert (tmp / "clones" / "code" / ".git").is_dir(), "the reviewer clones a repository it has no clone of"
        assert (tmp / "notes-seen").is_file(), "the hive's review notes are in the kickoff"
        print("  reviewer runner: kickoff written, its own clone made, stand-in command ran, verdict passed recorded, cause = the result")
        hive("cos", "emit", "task.closed", "--task", "t2")
        assert json.loads(hive("cos", "task", "t2"))["task"]["status"] == "done"
        # a task asked for in chat, proposed by the agent through the record; the chief of staff approves or rejects
        print("propose:")
        w1.tool("propose", "", "Summarize the journal\nOne page for Charlie, who asked in protobots.", "telegram:-500/42")
        w1.wait_for("OK: proposed p-")
        pid = re.search(r"proposed (p-[a-z0-9-]+)", w1.heard[-1]).group(1)
        st = json.loads(hive("cos", "state"))
        pr = st["proposals"][pid]
        assert pr["status"] == "pending" and pr["proposer"] == "w1", pr
        sub_ev = [json.loads(l) for l in hive("cos", "events", "--type", "proposal.submitted").splitlines() if l.strip()][-1]
        assert sub_ev.get("cause") == {"external": "telegram:-500/42"}, sub_ev.get("cause")
        hive("cos", "emit", "proposal.approved", "--data", json.dumps({"proposal_id": pid}))
        t = json.loads(hive("cos", "task", pid))["task"]
        assert t["title"] == "Summarize the journal" and t["status"] == "created" and t["created_by"] == "cos", t
        w1.wait_for(f"Your proposal {pid} was approved")
        hive("cos", "emit", "task.assigned", "--task", pid, "--data", json.dumps(
            {"to": "w1", "ext": {"lease": {"accept_within_seconds": 600, "checkin_every_seconds": 900}}}))
        w1.wait_for(f"New task {pid} for you")
        assert "One page for Charlie" in w1.heard[-1], w1.heard[-1]
        w1.tool("propose", "", "Rewrite everything in Rust", "Ben, private chat")
        w1.wait_for("OK: proposed p-")
        pid2 = re.search(r"proposed (p-[a-z0-9-]+)", w1.heard[-1]).group(1)
        hive("cos", "emit", "proposal.rejected", "--data", json.dumps({"proposal_id": pid2, "reason": "out of scope for this hive"}))
        w1.wait_for(f"Your proposal {pid2} was rejected: out of scope for this hive")
        print("  proposed (cause: the chat message), approved by the chief of staff into a task, assigned; a second rejected with its reason")

        # self-modification: the sidecar re-declares the agent when its code changes
        def harness():
            return next(a for a in json.loads(hive("cos", "actors")) if a["id"] == "w1")["declaration"]["harness"]

        def changes(before):
            for _ in range(30):
                h = harness()
                if h != before:
                    return h
                time.sleep(1)
            raise AssertionError(f"no re-declaration from {before!r}")
        h0 = harness()
        assert h0.startswith("iter not under git; hive adapter "), h0
        sh("git", "-C", str(w1.root), "init", "-q", env=gitenv)
        (w1.root / ".gitignore").write_text(".runtime/\n")
        (w1.root / "agent.py").write_text("print('v1')\n")
        sh("git", "-C", str(w1.root), "add", "-A", env=gitenv)
        sh("git", "-C", str(w1.root), "commit", "-q", "-m", "v1", env=gitenv)
        h1 = changes(h0)
        assert "@" in h1 and "+" not in h1.split(";")[0] and "flags missing" in h1, h1
        (w1.root / "skill.py").write_text("print('new')\n")                # uncommitted code
        h2 = changes(h1)
        assert h2.split(";")[0].startswith(h1.split(";")[0] + "+"), (h1, h2)
        (w1.root / "memory").mkdir()
        (w1.root / "memory" / "flags.json").write_text('{"goal_prompt": false}')
        h3 = changes(h2)
        assert "flags " in h3 and "flags missing" not in h3, h3
        (w1.root / "memory" / "notes.txt").write_text("not code")         # not code: no re-declaration
        time.sleep(5)
        assert harness() == h3, (h3, harness())
        print("  self-modification: re-declared on commit, uncommitted code and a flags file; not on other files")
        kinds = {(b["actor"], b["kind"]) for b in BEACONS}
        assert {("w1", "iter"), ("rv1", "reviewer")} <= kinds, kinds
        print(f"  beacons: {len(BEACONS)} from the sidecar (iter) and the reviewer runner (reviewer)")
        print("PASSED (t1: worker in sidecar mode, reviewer in spawn mode; t2: hive-task CLI and the reviewer runner): assigned, accepted, progress (tool and send), nudged, blocked, answered, unblocked, result refused "
              "until pushed, result with code+base pins, review failed, new result, review passed, closed")
    finally:
        for a in agents:
            a.stop()
        if gw:
            gw.terminate()
            gw.wait(10)
        scratch.down(pg.name)


if __name__ == "__main__":
    main()
