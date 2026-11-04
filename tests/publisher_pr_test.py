#!/usr/bin/env python3
"""publisher.py opens the pull request itself with --github-token (against a stub GitHub API): a new PR gets the
task's title, the base branch and the report's summary; one already open for the branch is reused; a refusal gives
the reason (the publisher then posts the compare link).

    python3 tests/publisher_pr_test.py
"""
import importlib.util
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
seen, mode = [], {"v": "new"}


class GH(BaseHTTPRequestHandler):
    def reply(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(b)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        seen.append(("POST", self.path, body, self.headers.get("Authorization")))
        if mode["v"] == "new":
            return self.reply(201, {"html_url": "https://github.com/o/r/pull/7"})
        if mode["v"] == "exists":
            return self.reply(422, {"message": "Validation Failed", "errors": [{"message": "A pull request already exists"}]})
        return self.reply(403, {"message": "Resource not accessible by integration"})

    def do_GET(self):
        seen.append(("GET", self.path, None, None))
        return self.reply(200, [{"html_url": "https://github.com/o/r/pull/5"}])

    def log_message(self, *a):
        pass


def main():
    srv = HTTPServer(("127.0.0.1", 0), GH)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ["HIVE_GITHUB_API"] = f"http://127.0.0.1:{srv.server_port}"
    spec = importlib.util.spec_from_file_location("publisher", HERE / "publisher.py")
    p = importlib.util.module_from_spec(spec); spec.loader.exec_module(p)

    report = "# Result\n\n## Summary\n\nThe parser now admits partial parses.\n\nSecond paragraph.\n\n## Details\n\nlots\n"
    assert p.summary_of(report) == "The parser now admits partial parses.\n\nSecond paragraph.", p.summary_of(report)
    assert p.summary_of("# no summary here\n") == ""

    url, why = p.open_pr("tok", "https://github.com/o/r", "hive/t-1", "main", "Do the thing", "body")
    assert url == "https://github.com/o/r/pull/7" and not why, (url, why)
    m, path, body, auth = seen[-1]
    assert path == "/repos/o/r/pulls" and body["head"] == "hive/t-1" and body["base"] == "main" \
        and body["title"] == "Do the thing" and auth == "Bearer tok", seen[-1]

    mode["v"] = "exists"
    url, why = p.open_pr("tok", "https://github.com/o/r.git", "hive/t-1", "main", "t", "b")
    assert url == "https://github.com/o/r/pull/5" and seen[-1][1] == "/repos/o/r/pulls?state=open&head=o:hive/t-1", seen[-1]

    mode["v"] = "refused"
    url, why = p.open_pr("tok", "https://github.com/o/r", "hive/t-1", "main", "t", "b")
    assert url is None and "403" in why, why
    assert p.open_pr("tok", "https://gitlab.com/o/r", "h", "main", "t", "b") == (None, "not a GitHub home")
    assert p.open_pr("tok", "https://github.com/o/r", "h", None, "t", "b")[0] is None

    body = p.pr_body({"task": "t-1", "report": None, "review": {"path": "projects/x/review.md"}}, "abcdef1234567890")
    assert "no ## Summary" in body and "merge commit" in body and "projects/x/review.md" in body, body
    print("PASSED: PR opened with title, base and body; an open one reused; a refusal explained; summary extracted")


if __name__ == "__main__":
    main()
