"""End to end: two Iter agents (a worker and a reviewer) on a scratch hive record, through the
adapters/iter channel and tool, called the way iter.py calls them (a fresh process per call, from
the agent's folder). Needs rootless Podman and a hive-record checkout:

    uv run --project ~/repos/hive-record python tests/iter_adapter_e2e.py

The task goes: assigned -> accepted -> progress (also through send) -> blocked -> answered ->
unblocked -> result with code and base pins -> review failed -> new result -> review passed -> closed.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
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
    def __init__(self, root: Path, actor: str, conf: dict):
        self.root, self.actor = root, actor
        (root / "channels").mkdir(parents=True)
        (root / "tools").mkdir()
        (root / ".hive").mkdir()
        shutil.copy(ADAPTER / "channels" / "hive.py", root / "channels" / "hive.py")
        shutil.copy(ADAPTER / "tools" / "hive_task.py", root / "tools" / "hive_task.py")
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
        try:
            os.kill(int((self.root / ".runtime" / "hive" / "pid").read_text()), 15)
        except Exception:
            pass


def main():
    tmp = Path(tempfile.mkdtemp(prefix="iter-e2e-"))
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
        pub = {a: sh(*HIVE, "keygen", "--out", str(tmp / f"{a}.key")).strip() for a in ("op", "cos", "w1", "rv1")}
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

        for a, cls in (("cos", "chief_of_staff"), ("w1", "worker"), ("rv1", "reviewer")):
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
        w1 = Agent(tmp / "iter-w1", "w1", conf("w1", {"code": str(code)}))
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
        w1.send("Started; nothing surprising.")
        w1.wait_for("OK: progress report on t1")
        w1.tool("block", "t1", "Which line should I add?\nOptions: a or b. Recommend a.", "decision")
        w1.wait_for("OK: t1 is blocked")
        apin = commit_ws("projects/p1/questions/t1-answer.md", "Add line a.\n", "answer")
        hive("cos", "emit", "task.answered", "--task", "t1", "--ref", f"answer={apin}")
        w1.wait_for("was answered")
        assert "Add line a." in w1.heard[-1], w1.heard[-1]
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
        t = json.loads(hive("cos", "task", "t1"))["task"]
        assert t["status"] == "done", t["status"]
        types = [json.loads(l)["type"] for l in hive("cos", "events").splitlines() if l.strip()]
        need = ["task.accepted", "task.reported", "task.blocked", "task.answered", "task.unblocked",
                "task.result_posted", "review.recorded", "task.closed"]
        assert all(x in types for x in need), types
        refs = [json.loads(l) for l in hive("cos", "events").splitlines() if '"task.result_posted"' in l][-1]["refs"]
        assert sorted(r["rel"] for r in refs) == ["base", "code", "result"], refs
        print("PASSED: assigned, accepted, progress (tool and send), blocked, answered, unblocked, result refused "
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
