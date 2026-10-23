"""Beacons (adapters/iter/channels/hive.py): what an agent's sidecar or the reviewer runner posts to the Floor,
built from the agent's files alone and signed with its record key. No record or gateway needed:

    uv run --project ~/repos/hive-record python tests/beacon_test.py

Checks the signature (prefix hive-beacon1), every field, that no prompt text leaves the agent, the
OpenClaw and reviewer forms, a problem for an unreadable folder, and the daemon thread posting by itself.
"""
import hashlib
import importlib.util
import json
import os
import shutil
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from hiverecord.keys import SigningKey, verify

HERE = Path(__file__).resolve().parent.parent
CHANNEL = HERE / "adapters" / "iter" / "channels" / "hive.py"
SECRET = "THE PROMPT TEXT MUST NEVER LEAVE THE AGENT"
GOT = []


class Catch(BaseHTTPRequestHandler):
    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        GOT.append((dict(self.headers), raw))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *a):
        pass


def load():
    spec = importlib.util.spec_from_file_location("hive_channel", CHANNEL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check_signed(headers, raw, key, hive_id):
    msg = f"hive-beacon1\n{hive_id}\n{headers['X-Hive-Signed-At']}\n{hashlib.sha256(raw).hexdigest()}".encode()
    assert headers["X-Hive-Key"] == key.public
    assert verify(key.public, msg, headers["X-Hive-Signature"]), "signature doesn't verify"
    assert not verify(key.public, msg.replace(b"hive-beacon1", b"hive1", 1), headers["X-Hive-Signature"])
    assert raw == json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":")).encode(), "body isn't canonical"
    assert SECRET.encode() not in raw, "prompt text leaked"


def main():
    tmp = Path(tempfile.mkdtemp(prefix="beacon-"))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Catch)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/api/beacon"
    key_file = tmp / "w.key"
    SigningKey.generate().to_file(key_file)
    key = SigningKey.from_file(key_file)
    now = time.time()
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))

    # an Iter agent's folder: the channel, its experience, a route hook with a closed and an open episode
    root = tmp / "agent"
    (root / "channels").mkdir(parents=True)
    shutil.copy(CHANNEL, root / "channels" / "hive.py")
    (root / "experience.json").write_text("[]")
    os.utime(root / "experience.json", (now - 42, now - 42))
    hook = [{"at": iso(now - 3 * 86400), "event": "routed", "episode": "old", "model": "m0"},   # ended long ago
            {"at": iso(now - 3 * 86400 + 60), "event": "ended", "episode": "old"},
            {"at": iso(now - 3600), "event": "routed", "episode": "e1", "model": "claude-opus-5-5", "key": "k", "task": "t"},
            {"at": iso(now - 3000), "event": "end_skipped_no_token", "episode": "e1"},
            {"at": iso(now - 2400), "event": "ended", "episode": "e1", "models": ["claude-opus-5-5"]},
            {"at": iso(now - 120), "event": "routed", "episode": "e2", "model": "claude-sonnet-5-5"}]
    (root / "route-hook.jsonl").write_text("\n".join(map(json.dumps, hook)) + "\nnot json\n")
    # its outer channel: one claimed request (whose prompt must stay here), two waiting, two answered
    ch = tmp / "iter-channel"
    for sub in ("inbox", "processing", "completed", "outbox"):
        (ch / sub).mkdir(parents=True)
    (ch / "processing" / "r1.json").write_text(json.dumps({"schema_version": 1, "identity": "Bot", "request_id": "r1",
        "prompt": SECRET, "prompt_sha256": "x", "source": {"chat_id": -5437945421, "message_id": 77, "update_id": 9}}))
    (ch / "processing" / "broken.json").write_text("{")
    for n in ("a", "b"):
        (ch / "inbox" / f"{n}.json").write_text(json.dumps({"prompt": SECRET}))
    for n, age in (("c1", 2 * 86400), ("c2", 600)):
        f = ch / "completed" / f"{n}.json"
        f.write_text(json.dumps({"response": SECRET}))
        os.utime(f, (now - age, now - age))
    conf = {"actor": "worker.bot", "hive_id": "test-hive", "key_file": str(key_file),
            "beacon": {"url": url, "every": 1, "iter_channel": str(ch)}}

    mod = load()
    mod.configure(root, tmp / "conf.json")
    mod._dirs()
    mod._put(mod.OUTBOX, {"action": "progress", "task": "", "text": SECRET})
    body = mod.beacon_body(conf, root, now - 500)
    assert mod.beacon_send(conf, body) == 204
    headers, raw = GOT[-1]
    check_signed(headers, raw, key, "test-hive")
    b = json.loads(raw)
    print("iter beacon:", json.dumps(b))
    assert b["v"] == 1 and b["actor"] == "worker.bot" and b["kind"] == "iter" and b["started"] == now - 500
    assert b["requests"] == [{"chat_id": -5437945421, "message_id": 77, "since": b["requests"][0]["since"]}]
    assert abs(b["requests"][0]["since"] - now) < 60
    assert b["queued"] == 2 and b["pending_actions"] == 1
    # the ctime of a file just written is now, whatever its mtime: completed answers count from when they moved in
    assert len(b["answers"]) == 2 and b["last_answer"] == b["answers"][-1]
    assert abs(b["last_step"] - (now - 42)) < 1 or b["last_step"] >= now - 42
    # a request this user can't read (a sidecar under another user than the agent's): there, but no ids
    if os.geteuid() != 0:
        locked = ch / "processing" / "locked.json"
        locked.write_text("{}")
        locked.chmod(0)
        rq = mod.beacon_body(conf, root, now - 500)["requests"]
        locked.chmod(0o600)
        locked.unlink()
        assert {"chat_id": None, "message_id": None} in [{k: r[k] for k in ("chat_id", "message_id")} for r in rq], rq
    eps = b["episodes"]
    assert [e[2] for e in eps] == ["claude-opus-5-5", "claude-sonnet-5-5"], eps
    assert eps[0][1] is not None and eps[1][1] is None and eps[0][0] < eps[1][0]
    assert b["problem"] is None
    assert "chat" not in b and "reviewing" not in b

    # an unreadable channel folder is a problem, not a crash (skipped when running as root)
    if os.geteuid() != 0:
        (ch / "processing").chmod(0)
        try:
            b2 = mod.beacon_body(conf, root, now)
        finally:
            (ch / "processing").chmod(0o755)
        assert b2["problem"] and "can't read" in b2["problem"] and "requests" not in b2, b2

    # an OpenClaw agent (hive-task's queue folder holds no channel): newest transcript, chat of the last session
    oc = tmp / "sessions"
    oc.mkdir()
    for n, age in (("s-old", 900), ("s-new", 30)):
        (oc / f"{n}.jsonl").write_text(SECRET)
        os.utime(oc / f"{n}.jsonl", (now - age, now - age))
    (oc / "sessions.json").write_text(json.dumps({
        "agent:main:telegram:group:-5437945421": {"sessionId": "s-old", "updatedAt": (now - 900) * 1000},
        "agent:main:telegram:direct:260710712": {"sessionId": "s-new", "updatedAt": (now - 30) * 1000}}))
    queue = tmp / "queue"
    queue.mkdir()
    oconf = {"actor": "worker.oc", "hive_id": "test-hive", "key_file": str(key_file),
             "beacon": {"url": url, "openclaw_sessions": str(oc)}}
    mod.configure(queue, tmp / "oc.json")
    ob = mod.beacon_body(oconf, queue, now)
    print("openclaw beacon:", json.dumps(ob))
    assert ob["kind"] == "openclaw" and ob["chat"] == {"chat_id": 260710712}
    assert ob["last_step"] >= now - 31 and "episodes" not in ob and "requests" not in ob
    (oc / "sessions.json").write_text("[not an object]")
    assert "chat" not in mod.beacon_body(oconf, queue, now)

    # the reviewer runner's form: kind reviewer, reviewing and problem from the runner
    rconf = {"actor": "reviewer.astra", "hive_id": "test-hive", "key_file": str(key_file), "beacon": {"url": url}}
    rb = mod.beacon_body(rconf, tmp / "rv", now, "reviewer",
                         {"reviewing": {"task": "t-1", "since": now - 5}, "problem": "no verdict from the review of t-0 (rc 1): not logged in"})
    print("reviewer beacon:", json.dumps(rb))
    assert rb["kind"] == "reviewer" and rb["reviewing"] == {"task": "t-1", "since": now - 5}
    assert rb["problem"].startswith("no verdict") and len(rb["problem"]) <= 200
    assert mod.beacon_body(rconf, tmp / "rv", now, "reviewer", {"reviewing": None, "problem": None})["problem"] is None

    # the daemon's thread posts by itself, every beacon.every seconds, with the extra fields
    mod.configure(root, tmp / "conf.json")
    n0 = len(GOT)
    mod.start_beacon({**rconf, "beacon": {"url": url, "every": 1}}, "reviewer", lambda: {"reviewing": None, "problem": "x"})
    end = time.time() + 10
    while len(GOT) < n0 + 2 and time.time() < end:
        time.sleep(0.2)
    assert len(GOT) >= n0 + 2, "the beacon thread didn't post twice"
    check_signed(*GOT[-1], key, "test-hive")
    assert json.loads(GOT[-1][1])["problem"] == "x"
    assert mod.start_beacon({"actor": "a"}) is None             # no beacon.url: no thread
    srv.shutdown()
    print("beacon test: OK")


if __name__ == "__main__":
    main()
