"""The Iter adapter's base for a result: the default branch at accept; repaired at result time when the work
doesn't descend from the recorded base (a clone left on an earlier task's branch: CODE_BASE_INVALID)."""
import importlib.util
import json
import os
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
GENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def sh(*a, cwd=None):
    return subprocess.run(a, cwd=cwd, env=GENV, check=True, capture_output=True, text=True).stdout.strip()


def main():
    tmp = Path(tempfile.mkdtemp())
    sh("git", "init", "-q", "--bare", "-b", "main", str(tmp / "o.git"))
    c = tmp / "clone"
    sh("git", "clone", "-q", str(tmp / "o.git"), str(c))
    def commit(m):
        (c / "f").write_text(m); sh("git", "add", "-A", cwd=c); sh("git", "commit", "-qm", m, cwd=c)
        return sh("git", "rev-parse", "HEAD", cwd=c)
    m1 = commit("main 1"); sh("git", "push", "-q", "origin", "HEAD:main", cwd=c); sh("git", "fetch", "-q", cwd=c)
    sh("git", "checkout", "-q", "-b", "hive/old", cwd=c); old = commit("an earlier task")   # the clone is left here
    spec = importlib.util.spec_from_file_location("hive_channel", HERE / "adapters" / "iter" / "channels" / "hive.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    m.configure(tmp / "agent"); m._dirs()
    reg = tmp / "reg.json"; reg.write_text(json.dumps({"version": 1, "repositories": {"workspace": {"local_path": "/x"}}}))
    h = m.Hive({"actor": "w", "registry": str(reg), "hive_url": "x", "hive_id": "x", "key_file": "x",
                "workspace": {"repository": "workspace", "clone": str(tmp / "ws"), "branch": "main"}})
    assert m.default_tip(h, str(c)) == m1, "accept: the default branch, not the clone's HEAD"
    sh("git", "checkout", "-q", "-b", "hive/new", m1, cwd=c); new = commit("the new task")
    assert m.base_for(h, str(c), new, m1, m1) == m1, "work from main: the recorded base stands"
    assert m.base_for(h, str(c), new, old, m1) == m1, "a base the work doesn't contain: where it left main"
    sh("git", "checkout", "-q", "hive/old", cwd=c); on_old = commit("built on the earlier task")
    assert m.base_for(h, str(c), on_old, m1, m1) == m1, "work built on another branch still descends from main's tip"
    print("PASSED: base at accept = the default branch; a recorded base the work doesn't contain is repaired at result")


if __name__ == "__main__":
    main()
