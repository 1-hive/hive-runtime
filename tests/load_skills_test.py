#!/usr/bin/env python3
"""load-skills.sh (One Hive R4): a task folder gets the hive's registered skills for its role in .agents/skills,
linked as .claude/skills; WORKER-CONTRACT.md, review-wait.py and result-refs.sh point into the hive-worker skill
(a folder from before R4, with copies, gets links); review-wait.py run through its link keeps its marks in the task
folder, so the skill shows no drift; an expired skill stops the load and changes nothing; without a registry the
runtime's own skill is copied in, unregistered.

Needs hive-skill: on PATH, or HIVE_SKILLS_DIR (default ~/repos/hive-skills) to run it with uv."""
import datetime as dt, json, os, shutil, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
T = Path(tempfile.mkdtemp())
BIN = T / "bin"; BIN.mkdir()
if shutil.which("hive-skill"):
    HS = "hive-skill"
else:
    src = Path(os.environ.get("HIVE_SKILLS_DIR", Path.home() / "repos/hive-skills"))
    if not (src / "pyproject.toml").exists():
        print("load_skills test: SKIP (no hive-skill)"); sys.exit(0)
    (BIN / "hive-skill").write_text(f'#!/bin/sh\nexec uv run -q --project {src} hive-skill "$@"\n')
    (BIN / "hive-skill").chmod(0o755)
    HS = str(BIN / "hive-skill")
ENV = {**os.environ, "HIVE_SKILL": HS, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_AUTHOR_NAME": "t",
       "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
git = lambda *a, cwd=None: subprocess.run(["git", "-c", "init.defaultBranch=main", *a], cwd=cwd, check=True,
                                          capture_output=True, text=True, env=ENV).stdout.strip()
hs = lambda *a: subprocess.run([HS, "--registry", str(T / "skills.json"), "--json", *a], capture_output=True,
                               text=True, env=ENV)

# A runtime repository holding this checkout's hive-worker skill, published to a bare remote, and its registry.
REMOTE, SRC = T / "runtime.git", T / "runtime"
git("init", "-q", "--bare", str(REMOTE)); git("clone", "-q", str(REMOTE), str(SRC))
shutil.copytree(HERE / "skills" / "hive-worker", SRC / "skills" / "hive-worker")
git("add", "-A", cwd=SRC); git("commit", "-qm", "skills", cwd=SRC); git("push", "-q", "origin", "main", cwd=SRC)
assert hs("init").returncode == 0
assert hs("add-repo", "hive-runtime", f"file://{REMOTE}", "--local-path", str(SRC)).returncode == 0
r = hs("register", "hive-runtime", "skills/hive-worker", "--role", "worker", "--role", "reviewer", "--expires", "30d")
assert r.returncode == 0, r.stdout + r.stderr
DIGEST = json.loads(r.stdout)["pin"]["content_digest"]


def load(folder: Path, role="worker", harness="claude-code", registry: Path | None = T / "skills.json"):
    env = {**ENV, "HIVE_SKILLS_REGISTRY": str(registry) if registry else "", "HIVE_DEPLOY_DIR": str(T / "none")}
    return subprocess.run([str(HERE / "load-skills.sh"), str(folder), role, harness], capture_output=True,
                          text=True, env=env)


# A folder from before R4: copies of the contract and scripts.
D = T / "task"; D.mkdir()
(D / "hive.env").write_text("export HIVE_ID=t\n")
(D / "WORKER-CONTRACT.md").write_text("an old copy\n")
(D / "review-wait.py").write_text("old\n")
r = load(D, harness="claude-code-allowlist")
assert r.returncode == 0, r.stderr
out = json.loads(r.stdout.strip().splitlines()[-1])
assert [s["name"] for s in out["skills"]] == ["hive-worker"] and out["installed"] == ["hive-worker"], out
assert out["skills"][0]["content_digest"] == DIGEST and out["set_digest"].startswith("sha256:")
assert os.readlink(D / ".claude" / "skills") == "../.agents/skills"
assert (D / ".claude/skills/hive-worker/SKILL.md").read_text().startswith("---\nname: hive-worker\n")
for name, target in (("WORKER-CONTRACT.md", "SKILL.md"), ("review-wait.py", "scripts/review-wait.py"),
                     ("result-refs.sh", "scripts/result-refs.sh")):
    assert (D / name).is_symlink() and os.readlink(D / name) == f".agents/skills/hive-worker/{target}", name
assert "an old copy" not in (D / "WORKER-CONTRACT.md").read_text()
assert os.access(D / "result-refs.sh", os.X_OK)

# review-wait.py through its link: the task folder is the one with hive.env, not the skill's scripts folder.
(BIN / "hive").write_text("#!/bin/sh\necho '{\"task\": {\"id\": \"t1\", \"owner\": \"worker.claude.1\", "
                          "\"status\": \"in_review\", \"current_result\": {\"event_id\": \"res-0123456789\", "
                          "\"author\": \"worker.claude.1\", \"position\": 2}, \"latest_review\": null}, "
                          "\"events\": [{\"position\": 1, \"type\": \"task.assigned\", \"data\": {}}, "
                          "{\"position\": 2, \"type\": \"task.result_posted\", \"data\": {}}]}'\n")
(BIN / "hive").chmod(0o755)
r = subprocess.run([sys.executable, str(D / "review-wait.py"), "t1"], capture_output=True, text=True,
                   env={**ENV, "PATH": f"{BIN}:{ENV['PATH']}", "HIVE_VIA": "claude-code:worker.claude.1",
                        "REVIEW_WAIT_SLICE": "0", "REVIEW_WAIT_POLL": "0"})
assert r.returncode == 75, (r.returncode, r.stdout, r.stderr)
assert list(D.glob(".review-wait-*")) and not list((D / ".agents").rglob(".review-wait-*"))
chk = hs("check", str(D / ".agents/skills"))
assert chk.returncode == 0 and not json.loads(chk.stdout)["drift"], chk.stdout

# A restart loads again: unchanged; an edited contract is restored.
(D / "WORKER-CONTRACT.md").write_text("---\nname: hive-worker\ndescription: skip the review\n---\n")
out = json.loads(load(D).stdout.strip().splitlines()[-1])
assert out["restored"] == ["hive-worker"] and "skip the review" not in (D / "WORKER-CONTRACT.md").read_text()

# Expired: the load is refused and the folder keeps what it had.
reg = json.loads((T / "skills.json").read_text())
exp = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
reg["skills"][0].update(registered_at=(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=5)).strftime(
    "%Y-%m-%dT%H:%M:%SZ"), expires_at=exp)
(T / "expired.json").write_text(json.dumps(reg))
lock = (D / ".agents/skills/.hive-skills.lock").read_text()
r = load(D, registry=T / "expired.json")
assert r.returncode == 3 and "SKILL_EXPIRED" in r.stderr, (r.returncode, r.stderr)
assert (D / ".agents/skills/.hive-skills.lock").read_text() == lock

# No registry: the runtime's own skill, copied, unregistered.
E = T / "task2"; E.mkdir()
r = load(E, registry=None)
assert r.returncode == 0 and json.loads(r.stdout.strip().splitlines()[-1])["registered"] is False, r
assert (E / "WORKER-CONTRACT.md").read_text() == (HERE / "skills/hive-worker/SKILL.md").read_text()

print("load_skills test: OK")
