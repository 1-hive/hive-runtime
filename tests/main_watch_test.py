"""main-watch.py on local repositories, with the record's reviewed commits stubbed:
first run, a merged reviewed branch, a fast-forward of a reviewed result, a direct commit, a rewrite."""
import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
GENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t"}


def sh(*cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, env=GENV, check=True, capture_output=True, text=True).stdout.strip()


def main():
    tmp = Path(tempfile.mkdtemp())
    os.environ["HIVE_WORK_DIR"] = str(tmp / "work")
    spec = importlib.util.spec_from_file_location("main_watch", HERE / "main-watch.py")
    mw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mw)
    mw.STATE = tmp / "state.json"
    reviewed: set[str] = set()
    mw.reviewed_commits = lambda: {"app": reviewed}
    sh("git", "init", "-q", "--bare", "-b", "main", str(tmp / "origin.git"))
    work = tmp / "w"
    sh("git", "clone", "-q", str(tmp / "origin.git"), str(work))

    def commit(msg):
        (work / "f").write_text(msg)
        sh("git", "add", "-A", cwd=work)
        sh("git", "commit", "-q", "-m", msg, cwd=work)
        return sh("git", "rev-parse", "HEAD", cwd=work)
    commit("base")
    sh("git", "push", "-q", "origin", "HEAD:main", cwd=work)
    sh("git", "clone", "-q", "--bare", str(tmp / "origin.git"), str(tmp / "clone.git"))
    spec_arg = f"app={tmp / 'clone.git'}"

    def run():
        buf = io.StringIO()
        sys.argv = ["main-watch.py", spec_arg]
        with contextlib.redirect_stdout(buf):
            mw.main()
        return buf.getvalue().strip()

    assert run() == "", "first run only records"
    # a reviewed branch merged with a merge commit
    sh("git", "checkout", "-q", "-b", "hive/t1", cwd=work)
    reviewed.add(commit("t1 work"))
    sh("git", "checkout", "-q", "main", cwd=work)
    sh("git", "merge", "-q", "--no-ff", "-m", "Merge pull request #1", "hive/t1", cwd=work)
    sh("git", "push", "-q", "origin", "HEAD:main", cwd=work)
    assert run() == "", "merge of a reviewed result passes"
    # fast-forward of a reviewed result
    reviewed.add(commit("t2 work"))
    sh("git", "push", "-q", "origin", "HEAD:main", cwd=work)
    assert run() == "", "fast-forward of a reviewed result passes"
    # a direct commit
    commit("hotfix straight to main")
    sh("git", "push", "-q", "origin", "HEAD:main", cwd=work)
    out = run()
    assert "1 commit(s) reached main without a passed review" in out and "hotfix" in out, out
    # a rewrite
    sh("git", "reset", "-q", "--hard", "HEAD~2", cwd=work)
    commit("rewritten")
    sh("git", "push", "-q", "-f", "origin", "HEAD:main", cwd=work)
    out = run()
    assert "was rewritten" in out, out
    print("PASSED: first run silent; merge commit and fast-forward of reviewed results pass; a direct commit and a rewrite are flagged")


if __name__ == "__main__":
    main()
