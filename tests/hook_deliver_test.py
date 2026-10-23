"""adapters/openclaw/hook-deliver.py against a stand-in gateway webhook."""
import json
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
got = []


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        got.append((self.path, dict(self.headers), body))
        ok = self.headers.get("Authorization") == "Bearer s3cret"
        self.send_response(200 if ok else 401)
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True, "runId": "r1"} if ok else {"ok": False}).encode())

    def log_message(self, *a):
        pass


def main():
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/hooks/agent"
    tok = Path(tempfile.mkdtemp()) / "t"
    tok.write_text("s3cret\n")
    run = lambda *a: subprocess.run([sys.executable, str(HERE / "adapters" / "openclaw" / "hook-deliver.py"), *a],
                                    capture_output=True, text=True)
    r = run("--url", url, "--token-file", str(tok), "New task t1 for you: x")
    assert r.returncode == 0 and "admitted: run r1" in r.stdout, (r.stdout, r.stderr)
    path, hdr, body = got[-1]
    assert path == "/hooks/agent" and body["agentId"] == "main" and body["deliver"] is False, body
    assert body["message"].endswith("New task t1 for you: x") and "hive-task" in body["message"], body
    assert hdr.get("Idempotency-Key") and hdr["Content-Type"] == "application/json"
    tok.write_text("wrong")
    r = run("--url", url, "--token-file", str(tok), "x")
    assert r.returncode != 0 and "HTTP 401" in r.stderr, r.stderr
    r = run("--url", "http://127.0.0.1:9/hooks/agent", "--token-file", str(tok), "--timeout", "3", "x")
    assert r.returncode != 0, "no gateway: non-zero, so the sidecar keeps the message"
    print("PASSED: delivered with the bearer token, agent main, deliver false, an idempotency key; "
          "a refused token or no gateway exits non-zero")


if __name__ == "__main__":
    main()
