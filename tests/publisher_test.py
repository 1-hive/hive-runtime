"""publisher.py and the sidecar's staging (adapters/iter/channels/hive.py stage) on local repositories, with
the record's passed reviews stubbed: a hive copy made and mirrored (hive/* left alone), the first run only
recording, a staged result published after review, a secret or denied line kept back, a missing commit and a
missing key reported, and a rewritten result staged beside the earlier one, never over it.

    python tests/publisher_test.py
"""
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp())
os.environ["HIVE_WORK_DIR"] = str(TMP / "work")
sys.path.insert(0, str(HERE))


def sh(*cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    assert r.returncode == 0, f"{cmd}: {r.stderr}"
    return r.stdout.strip()


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def commit(work, name, text, msg):
    (work / name).write_text(text)
    sh("git", "add", "-A", cwd=work)
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", msg, cwd=work)
    return sh("git", "rev-parse", "HEAD", cwd=work)


def main():
    pub = load("publisher", HERE / "publisher.py")
    home, copy, work, keys = TMP / "home.git", TMP / "repos" / "app.git", TMP / "work-clone", TMP / "keys"
    sh("git", "init", "-q", "--bare", "-b", "main", str(home))
    sh("git", "clone", "-q", str(home), str(work))
    base = commit(work, "a.txt", "one\n", "start")
    sh("git", "push", "-q", "origin", "HEAD:main", cwd=work)
    sh("git", "push", "-q", str(home), f"{base}:refs/heads/hive/upstream-only", cwd=work)
    reg = TMP / "registry.json"
    reg.write_text(json.dumps({"version": 1, "repositories": {
        "app": {"fetch_urls": [f"file://{copy}", f"file://{home}"], "object_format": "sha1",
                "allowed_ref_patterns": ["refs/heads/*"]},
        "workspace": {"fetch_urls": [f"file://{TMP}/ws.git"], "object_format": "sha1",
                      "allowed_ref_patterns": ["refs/heads/*"]}}}))
    results: list[dict] = [{"task": "t-old", "repository": "app", "code": base, "base": base}]
    pub.passed_results = lambda: list(results)

    def run(*extra):
        sys.argv = ["publisher.py", "--registry", str(reg), *extra]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            pub.main()
        return buf.getvalue()

    # --init-only: the copy is made and mirrored, the record isn't read
    pub.passed_results = lambda: (_ for _ in ()).throw(AssertionError("init-only read the record"))
    assert run("--init-only") == ""
    pub.passed_results = lambda: list(results)
    # first run: earlier passed results are only recorded
    out = run("--keys", str(keys))
    assert out == "", out
    assert sh("git", "-C", str(copy), "rev-parse", "main") == base
    assert sh("git", "-C", str(copy), "symbolic-ref", "HEAD") == "refs/heads/main"
    assert not sh("git", "-C", str(copy), "for-each-ref", "refs/heads/hive"), "hive/* must not be mirrored"
    assert sh("git", "-C", str(copy), "config", "receive.denyNonFastForwards") == "true"
    assert (copy / "hooks" / "pre-receive").read_bytes() == (HERE / "git" / "pre-receive").read_bytes()
    assert not Path(TMP, "ws.git").exists(), "a repository without a home isn't staged"

    # the sidecar stages a result in the copy; a rewritten one goes beside it
    side = load("hive_channel", HERE / "adapters" / "iter" / "channels" / "hive.py")
    side.LOG = TMP / "side.log"

    class H:
        pass
    H.reg, H.env = json.loads(reg.read_text()), dict(os.environ)
    good = commit(work, "b.txt", "two\n", "work")
    assert side.hive_copy(H, "app") == f"file://{copy}" and side.hive_copy(H, "workspace") is None
    assert side.stage(H, str(work), good, "app", "t-1") == "hive/t-1"
    assert side.stage(H, str(work), good, "app", "t-1") == "hive/t-1"          # same commit again: fine
    sh("git", "reset", "-q", "--hard", base, cwd=work)
    other = commit(work, "b.txt", "two, rewritten\n", "work again")
    assert side.stage(H, str(work), other, "app", "t-1") == "hive/t-1-r2"
    assert sh("git", "-C", str(copy), "rev-parse", "hive/t-1") == good       # the reviewed one stays reachable

    # no key: reported once, nothing pushed
    results.append({"task": "t-1", "repository": "app", "code": good, "base": base})
    out = run("--keys", str(keys))
    assert "no push key for app" in out, out
    assert run("--keys", str(keys)) == "", "the same alert twice"
    keys.mkdir()
    (keys / "default").write_text("not used for file:// homes\n")

    # dry run, then the real thing: the reviewed commit reaches the home's hive/t-1, once
    assert "would publish app" in run("--keys", str(keys), "--dry-run")
    out = run("--keys", str(keys))
    assert "Reviewed and published: t-1" in out and "into main" in out, out
    assert sh("git", "-C", str(home), "rev-parse", "hive/t-1") == good
    assert run("--keys", str(keys)) == ""

    # a secret, or a denied pattern, keeps a result in the hive
    sh("git", "checkout", "-q", "-B", "w", good, cwd=work)
    leak = commit(work, "c.txt", "token = ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8\n", "oops")
    side.stage(H, str(work), leak, "app", "t-2")
    results.append({"task": "t-2", "repository": "app", "code": leak, "base": good})
    out = run("--keys", str(keys))
    assert "NOT publishing t-2" in out, out
    assert subprocess.run(["git", "-C", str(home), "rev-parse", "-q", "--verify", "hive/t-2"], capture_output=True, check=False).returncode
    ids = commit(work, "d.txt", "chat -1001234567890\n", "real id")
    side.stage(H, str(work), ids, "app", "t-3")
    results.append({"task": "t-3", "repository": "app", "code": ids, "base": leak})
    assert "NOT publishing t-3" in run("--keys", str(keys), r"--deny=-100\d{10}")

    # already in its home under hive/<task> (pushed the old way): counted as published, silently
    sh("git", "push", "-q", str(home), f"{other}:refs/heads/hive/t-5", cwd=work)
    results.append({"task": "t-5", "repository": "app", "code": other, "base": base})
    assert run("--keys", str(keys), r"--deny=-100\d{10}") == ""

    # a reviewed commit the copy doesn't have
    results.append({"task": "t-4", "repository": "app", "code": "1" * 40, "base": base})
    assert "isn't in the hive copy" in run("--keys", str(keys), r"--deny=-100\d{10}")
    print("PASSED: copy made and mirrored without hive/*; first run only records; staged beside, never over; "
          "no key reported once; reviewed commit published once with its PR target; secrets and denied lines "
          "kept back; a missing commit reported")


if __name__ == "__main__":
    main()
