#!/usr/bin/env python3
"""publisher.py --merge-pr: an operator's 🔀 Merge tap merges a hive pull request on its home (against a stub GitHub
API), with a merge commit and GitHub's own head check (sha), and only a hive/* PR into a staged home of this hive, still
open and still at the reviewed commit. A refusal says why; a token that may not merge says what it needs.

    python3 tests/publisher_merge_test.py
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
SHA, OTHER = "a" * 40, "b" * 40
seen = []
pr = {"state": "open", "merged": False, "head": {"ref": "hive/t-1", "sha": SHA}, "base": {"ref": "main"}, "title": "T"}
put = {"code": 200, "body": {"merged": True, "sha": "c" * 40, "message": "Pull Request successfully merged"}}


class GH(BaseHTTPRequestHandler):
    def reply(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        seen.append(("GET", self.path, None, self.headers.get("Authorization")))
        return self.reply(200, pr)

    def do_PUT(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        seen.append(("PUT", self.path, body, self.headers.get("Authorization")))
        return self.reply(put["code"], put["body"])

    def log_message(self, *a):
        pass


def main():
    srv = HTTPServer(("127.0.0.1", 0), GH)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ["HIVE_GITHUB_API"] = f"http://127.0.0.1:{srv.server_port}"
    spec = importlib.util.spec_from_file_location("publisher", HERE / "publisher.py")
    p = importlib.util.module_from_spec(spec); spec.loader.exec_module(p)
    reg = {"repositories": {
        "r": {"fetch_urls": ["file:///var/lib/hive/repos/r.git", "https://github.com/O/R.git"]},
        "local": {"fetch_urls": ["file:///var/lib/hive/repos/local.git"]}}}
    url = "https://github.com/o/r/pull/7"

    out = p.merge_pr("tok", reg, url, SHA)
    assert out["ok"] and out["merged_sha"] == "c" * 40 and out["base"] == "main" and out["head"] == "hive/t-1", out
    m, path, body, auth = seen[-1]
    assert m == "PUT" and path == "/repos/o/r/pulls/7/merge" and auth == "Bearer tok", seen[-1]
    assert body["sha"] == SHA and body["merge_method"] == "merge", body

    # Only a home of this hive; only a full commit and a PR URL.
    seen.clear()
    assert "isn't a home" in p.merge_pr("tok", reg, "https://github.com/someone/else/pull/1", SHA)["why"]
    assert not p.merge_pr("tok", reg, url, SHA[:10])["ok"]
    assert not p.merge_pr("tok", reg, "https://github.com/o/r/pull/7/files", SHA)["ok"]
    assert not seen, "nothing asked of GitHub for those"

    # Refusals before merging: someone pushed since, a closed (superseded) PR, an already merged one, not a hive branch.
    for change, why in [({"head": {"ref": "hive/t-1", "sha": OTHER}}, "someone pushed to it"),
                        ({"state": "closed"}, "closed"), ({"merged": True, "state": "closed"}, "already merged"),
                        ({"head": {"ref": "feature", "sha": SHA}}, "isn't one the hive published")]:
        keep = dict(pr)
        pr.update(change)
        seen.clear()
        out = p.merge_pr("tok", reg, url, SHA)
        assert not out["ok"] and why in out["why"], (change, out)
        assert all(s[0] == "GET" for s in seen), "never merged"
        pr.clear(); pr.update(keep)

    # GitHub refuses: the token may not merge; the head moved in between; not mergeable (a conflict, a check).
    for code, why in [(403, "Contents: write"), (409, "head moved"), (405, "won't merge it")]:
        put.update(code=code, body={"message": "nope"})
        out = p.merge_pr("tok", reg, url, SHA)
        assert not out["ok"] and why in out["why"], (code, out)
    print("PASSED: --merge-pr merges a hive PR with a merge commit at the reviewed head; refuses other homes, other "
          "branches, closed or merged PRs and a moved head; explains GitHub's refusals")


if __name__ == "__main__":
    main()
