#!/usr/bin/env python3
"""publisher.py's tidying and privacy gate (stub GitHub API, local repositories):
- close_superseded closes an open hive/* PR whose commit is in the new PR's history, and one whose task the report
  names on a "Supersedes:" line (a squashed redo), comments, deletes the branch; leaves unrelated, non-hive and
  fork PRs alone;
- private_scan finds a 6-word run shared with the private files, also wrapped over lines, names file:line and
  never the text; is_public counts anything GitHub doesn't confirm as private as public.

    python3 tests/publisher_tidy_test.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp())
os.environ["HIVE_WORK_DIR"] = str(TMP / "work")
sys.path.insert(0, str(HERE))
calls, open_prs, repo_private = [], [], {"v": False}


class GH(BaseHTTPRequestHandler):
    def reply(self, code, obj=None):
        b = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(b)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n)) if n else None

    def do_GET(self):
        calls.append(("GET", self.path, None))
        if self.path.startswith("/repos/o/r/pulls"):
            return self.reply(200, open_prs)
        if self.path == "/repos/o/r":
            return self.reply(200, {"private": repo_private["v"]})
        return self.reply(404, {"message": "Not Found"})

    def do_POST(self):
        calls.append(("POST", self.path, self.body())); self.reply(201, {"id": 1})

    def do_PATCH(self):
        calls.append(("PATCH", self.path, self.body())); self.reply(200, {"state": "closed"})

    def do_DELETE(self):
        calls.append(("DELETE", self.path, None)); self.reply(204)

    def log_message(self, *a):
        pass


def sh(*cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    assert r.returncode == 0, f"{cmd}: {r.stderr}"
    return r.stdout.strip()


def commit(work, name, text, msg):
    (work / name).write_text(text)
    sh("git", "add", "-A", cwd=work)
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", msg, cwd=work)
    return sh("git", "rev-parse", "HEAD", cwd=work)


def pr(n, ref, sha, full="o/r"):
    return {"number": n, "head": {"ref": ref, "sha": sha, "repo": {"full_name": full}}}


def main():
    srv = HTTPServer(("127.0.0.1", 0), GH)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ["HIVE_GITHUB_API"] = f"http://127.0.0.1:{srv.server_port}"
    spec = importlib.util.spec_from_file_location("publisher", HERE / "publisher.py")
    p = importlib.util.module_from_spec(spec); spec.loader.exec_module(p)

    # history: main -> p1 -> p3 -> p4; other branch off main; p4b = squashed redo off main
    work = TMP / "w"
    sh("git", "init", "-q", "-b", "main", str(work))
    base = commit(work, "a.txt", "a\n", "base")
    p1 = commit(work, "b.txt", "p1\n", "p1")
    p3 = commit(work, "c.txt", "p3\n", "p3")
    p4 = commit(work, "d.txt", "p4\n", "p4")
    sh("git", "checkout", "-q", "-b", "other", base, cwd=work)
    other = commit(work, "e.txt", "other\n", "other")
    sh("git", "checkout", "-q", "-b", "redo", base, cwd=work)
    redo = commit(work, "f.txt", "redo\n", "redo")

    open_prs[:] = [pr(5, "hive/t-p1", p1), pr(6, "hive/t-p3", p3), pr(7, "hive/t-other", other),
                   pr(8, "hive/t-p4", p4), pr(10, "feature/x", p1), pr(11, "hive/t-p1", p1, full="someone/fork")]
    closed, problems = p.close_superseded("tok", "https://github.com/o/r", work, "hive/t-p4", p4,
                                          "https://github.com/o/r/pull/8", set())
    assert closed == ["#5", "#6"] and not problems, (closed, problems)
    deleted = [c[1] for c in calls if c[0] == "DELETE"]
    assert deleted == ["/repos/o/r/git/refs/heads/hive/t-p1", "/repos/o/r/git/refs/heads/hive/t-p3"], deleted
    comments = [c for c in calls if c[0] == "POST"]
    assert all("Superseded by #8" in c[2]["body"] for c in comments) and len(comments) == 2, comments
    assert [c[1] for c in calls if c[0] == "PATCH"] == ["/repos/o/r/pulls/5", "/repos/o/r/pulls/6"]

    # the squashed redo: not in its history, so only the Supersedes line closes #8 (and its -r2 variant)
    report = "# Result\n\nSupersedes: t-p4\n\n## Summary\n\nredo\n"
    assert p.supersedes(report) == {"t-p4"} and p.supersedes("- **Supersedes:** t-a, t-b.c\n") == {"t-a", "t-b.c"}
    calls.clear()
    open_prs[:] = [pr(8, "hive/t-p4", p4), pr(12, "hive/t-p4-r2", p4), pr(7, "hive/t-other", other)]
    closed, _ = p.close_superseded("tok", "https://github.com/o/r", work, "hive/t-redo", redo,
                                   "https://github.com/o/r/pull/9", p.supersedes(report))
    assert closed == ["#8", "#12"], closed
    calls.clear()
    assert p.close_superseded("tok", "https://github.com/o/r", work, "hive/t-redo", redo,
                              "https://github.com/o/r/pull/9", set()) == ([], [])
    assert not [c for c in calls if c[0] != "GET"], calls
    assert p.close_superseded("", "https://github.com/o/r", work, "h", redo, "u/9", {"t-p4"}) == ([], [])

    # private text
    priv = TMP / "private"; priv.mkdir()
    (priv / "notes.md").write_text("Met Sam at the clinic about the second biopsy results today.\n")
    grams = p.private_grams(priv)
    sh("git", "checkout", "-q", "-b", "leak", base, cwd=work)
    clean = commit(work, "ok.md", "The parser met its target at the clinic benchmark.\n", "clean")
    leak = commit(work, "fixture.json", '{"x": 1,\n "text": "about the second\n biopsy results today"}\n', "leak")
    assert p.private_scan(work, base, clean, grams) == []
    hits = p.private_scan(work, clean, leak, grams)
    assert hits == ["fixture.json:2"], hits
    assert p.private_scan(work, base, leak, grams) == ["fixture.json:2"]
    assert p.private_scan(work, base, leak, set()) == []

    repo_private["v"] = True
    assert p.is_public("tok", "https://github.com/o/r") is False
    repo_private["v"] = False
    assert p.is_public("tok", "https://github.com/o/r") is True
    assert p.is_public("", "https://github.com/o/r") is True              # unknown: public
    assert p.is_public("tok", "https://github.com/o/missing") is True     # error: public
    assert p.is_public("tok", "file:///srv/x.git") is True
    print("PASSED: superseded PRs closed by history and by Supersedes line, others left; private text found by "
          "file:line across wrapped lines; unknown visibility counts as public")


if __name__ == "__main__":
    main()
