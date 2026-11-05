#!/usr/bin/env python3
"""new-repo.py against a stub GitHub API: creates the repository, adds the publisher's key, registers it staged
and commits the registry to the registry repository; a rerun changes nothing; a private repository, a taken id
and a refused token are errors. registry-pull.sh's validity check is exercised through publish_registry's output.

    python3 tests/new_repo_test.py
"""
import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("new_repo", HERE / "new-repo.py")
nr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nr)

state = {"repos": {}, "keys": {}, "collab": {}, "invites": {}, "calls": [], "token_ok": True}


class GH(BaseHTTPRequestHandler):
    def reply(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(b)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n)) if n else None

    def do_GET(self):
        state["calls"].append(("GET", self.path))
        if self.path == "/user":
            return self.reply(200, {"login": "agents"}) if state["token_ok"] else self.reply(401, {"message": "Bad credentials"})
        parts = self.path.strip("/").split("/")
        full = "/".join(parts[1:3])
        if parts[3:4] == ["collaborators"]:
            return self.reply(204, None) if parts[4] in state["collab"].get(full, []) else self.reply(404, {"message": "Not Found"})
        if parts[3:4] and parts[3].startswith("invitations"):
            return self.reply(200, [{"invitee": {"login": l}} for l in state["invites"].get(full, [])])
        if parts[-1] == "keys":
            return self.reply(200, state["keys"].get(full, []))
        r = state["repos"].get(full.lower())
        return self.reply(200, r) if r else self.reply(404, {"message": "Not Found"})

    def do_POST(self):
        b = self.body()
        state["calls"].append(("POST", self.path, b))
        if self.path == "/user/repos":
            full = f"agents/{b['name']}"
            state["repos"][full.lower()] = {"full_name": full, "html_url": f"https://github.com/{full}",
                                            "clone_url": f"https://github.com/{full}.git", "private": b["private"]}
            return self.reply(201, state["repos"][full.lower()])
        if self.path.endswith("/keys"):
            full = "/".join(self.path.strip("/").split("/")[1:3])
            state["keys"].setdefault(full, []).append({"key": b["key"] + " ignored-comment"})
            return self.reply(201, {"id": 1})
        return self.reply(404, {"message": "Not Found"})

    def do_PUT(self):
        b = self.body()
        state["calls"].append(("PUT", self.path, b))
        parts = self.path.strip("/").split("/")
        state["invites"].setdefault("/".join(parts[1:3]), []).append(parts[4])
        return self.reply(201, {"id": 7})

    def log_message(self, *a):
        pass


def run(tmp, *extra):
    sys.argv = ["new-repo.py", "--registry", str(tmp / "registry.json"), "--github-token", str(tmp / "token"),
                "--keys", str(tmp / "keys"), "--copies", "/var/lib/hive/repos", "--clones", "/var/lib/hive/clones",
                "--registry-repo", str(tmp / "hive-registry.git"), *extra]
    return nr.main()


def main():
    srv = HTTPServer(("127.0.0.1", 0), GH)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    nr.API = f"http://127.0.0.1:{srv.server_port}"
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        (tmp / "token").write_text("ghp_test\n")
        (tmp / "registry.json").write_text(json.dumps({"repositories": {"workspace": {
            "fetch_urls": ["file:///var/lib/hive/repos/workspace.git"], "object_format": "sha1",
            "allowed_ref_patterns": ["refs/heads/*"], "local_path": "/var/lib/hive/repos/workspace.git"}}}))

        # a dry run writes nothing
        assert run(tmp, "--name", "MeTTaDinC", "--dry-run") == 0
        assert not state["repos"] and not (tmp / "keys").exists() and "mettadinc" not in (tmp / "registry.json").read_text()

        # the real run: created, key added, registered staged, registry committed
        assert run(tmp, "--name", "MeTTaDinC", "--description", "MeTTaDinC on Cetta", "--collaborator", "glicerico",
                   "--collaborator", "@glicerico", "--collaborator", "agents") == 0
        puts = [c for c in state["calls"] if c[0] == "PUT"]
        assert puts == [("PUT", "/repos/agents/MeTTaDinC/collaborators/glicerico", {"permission": "push"})], puts
        assert "agents/mettadinc" in state["repos"], state["repos"]
        kp = [c for c in state["calls"] if c[0] == "POST" and c[1].endswith("/keys")]
        assert len(kp) == 1 and kp[0][2]["read_only"] is False, kp
        reg = json.loads((tmp / "registry.json").read_text())
        e = reg["repositories"]["mettadinc"]
        assert e["fetch_urls"] == ["file:///var/lib/hive/repos/mettadinc.git", "https://github.com/agents/MeTTaDinC.git"], e
        assert e["local_path"] == "/var/lib/hive/clones/mettadinc"
        assert '"fetch_urls": ["file:///var/lib/hive/repos/mettadinc.git", "https://github.com/agents/MeTTaDinC.git"]' \
            in (tmp / "registry.json").read_text()
        assert (tmp / "keys" / "mettadinc").stat().st_mode & 0o777 == 0o600
        shown = subprocess.run(["git", "-C", str(tmp / "hive-registry.git"), "show", "main:registry.json"],
                               capture_output=True, text=True, check=True).stdout
        assert json.loads(shown) == reg
        head = subprocess.run(["git", "-C", str(tmp / "hive-registry.git"), "rev-parse", "main"], capture_output=True, text=True).stdout

        # a rerun does nothing new
        n = len([c for c in state["calls"] if c[0] == "POST"])
        assert run(tmp, "--name", "MeTTaDinC", "--collaborator", "glicerico") == 0
        assert len([c for c in state["calls"] if c[0] in ("POST", "PUT")]) == n + 1
        # an existing repository gets a later collaborator; one already on it is left alone; a bad login is an error
        state["collab"]["agents/MeTTaDinC"] = ["bob"]
        assert run(tmp, "--name", "MeTTaDinC", "--collaborator", "bob", "--collaborator", "carol") == 0
        assert [c[1] for c in state["calls"] if c[0] == "PUT"][-1].endswith("/collaborators/carol")
        assert len([c for c in state["calls"] if c[0] == "PUT"]) == 2
        assert run(tmp, "--name", "MeTTaDinC", "--collaborator", "bad/login") == 1
        assert subprocess.run(["git", "-C", str(tmp / "hive-registry.git"), "rev-parse", "main"], capture_output=True, text=True).stdout == head

        # errors: a taken id elsewhere, a private repository, a bad token, a bad name
        reg["repositories"]["other"] = {**e, "fetch_urls": ["https://github.com/someone/other.git"]}
        (tmp / "registry.json").write_text(json.dumps(reg))
        assert run(tmp, "--name", "other") == 1
        state["repos"]["agents/secret"] = {"full_name": "agents/secret", "clone_url": "", "private": True}
        assert run(tmp, "--name", "secret") == 1
        assert run(tmp, "--name", "bad name") == 1
        state["token_ok"] = False
        assert run(tmp, "--name", "fresh") == 1
        assert "agents/fresh" not in state["repos"]

        # --publish-only commits a changed registry
        state["token_ok"] = True
        assert run(tmp, "--publish-only") == 0
        assert subprocess.run(["git", "-C", str(tmp / "hive-registry.git"), "rev-parse", "main"], capture_output=True, text=True).stdout != head
        # --merge-registry: a source copy takes what was registered on the host, keeps its own entries
        src = tmp / "source.json"
        src.write_text(json.dumps({"repositories": {"workspace": {"fetch_urls": ["mine"]}}}))
        assert nr.merge_registry(src, json.loads((tmp / "registry.json").read_text())) == ["mettadinc", "other"]
        got = json.loads(src.read_text())["repositories"]
        assert got["workspace"] == {"fetch_urls": ["mine"]} and got["mettadinc"]["local_path"] == "/var/lib/hive/clones/mettadinc"
        assert nr.merge_registry(src, json.loads((tmp / "registry.json").read_text())) == []
    print("new_repo_test: ok")


if __name__ == "__main__":
    main()
