#!/usr/bin/env python3
"""The Iter channel hands an agent the files attached beside its order (orders/<name>.md: orders/<name>/), copied into
its folder, and names the sidecar's paths as the agent sees them (config "agent_paths"). On 11-09 ProtoMegaBot2 blocked
on the review attached to its M0 review-fixes order, which only the order's text reached, and looked for its clone at
the sidecar's host path (/opt/proto-hive-runtime/...), which its container mounts at /hive/....

    python3 tests/order_attachments_test.py
"""
import importlib.util, os, subprocess, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
tmp = Path(tempfile.mkdtemp())
spec = importlib.util.spec_from_file_location("hive_channel", HERE / "adapters/iter/channels/hive.py")
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
root = tmp / "agent"; root.mkdir()
mod.configure(root, tmp / "conf.json"); mod._dirs()

ws = tmp / "ws"
g = lambda *a: subprocess.run(["git", "-C", str(ws), *a], check=True, capture_output=True, text=True).stdout.strip()
subprocess.run(["git", "init", "-q", str(ws)], check=True)
g("config", "user.email", "t@t"); g("config", "user.name", "t")
(ws / "projects/m/orders/o1").mkdir(parents=True)
(ws / "projects/m/orders/o1.md").write_text("# Order\nRead the attached review.\n")
(ws / "projects/m/orders/o1/review.md").write_text("F1 ... F6\n")
(ws / "projects/m/orders/o1/fixtures").mkdir()
(ws / "projects/m/orders/o1/fixtures/a.bin").write_bytes(bytes(range(256)))
(ws / "projects/m/orders/o2.md").write_text("# Order without attachments\n")
(ws / "projects/m/orders/o10").mkdir()
(ws / "projects/m/orders/o10/other.md").write_text("another order's file\n")
g("add", "-A"); g("commit", "-qm", "orders"); sha = g("rev-parse", "HEAD")

h = mod.Hive.__new__(mod.Hive)
h.c, h.env, h.ws = {}, dict(os.environ), {"repository": "workspace", "clone": str(ws), "branch": "main"}
pin = lambda p: {"repository": "workspace", "commit_oid": "sha1:" + sha, "path": p}

files = h.attachments("t-1", pin("projects/m/orders/o1.md"))
dest = root / ".runtime/hive/attachments/t-1"
assert sorted(files) == [dest / "fixtures/a.bin", dest / "review.md"], files
assert (dest / "review.md").read_text() == "F1 ... F6\n" and (dest / "fixtures/a.bin").read_bytes() == bytes(range(256))
text = h.order_text("t-1", pin("projects/m/orders/o1.md"))
assert "Read the attached review." in text and str(dest / "review.md") in text and "Attached to the order" in text, text
assert h.order_text("t-2", pin("projects/m/orders/o2.md")).endswith("# Order without attachments\n")
assert h.attachments("t-x", pin("projects/m/orders/o1")) == [] and h.attachments("t-y", None) == []
assert h.attachments("t-z", {**pin("projects/m/orders/o1.md"), "repository": "other"}) == []

mod.AGENT_PATHS[:] = [(str(root.parent), "/hive/bot")]
mod.tell(f"Order: see {dest / 'review.md'}")
got = [p.read_text() for p in sorted((root / ".runtime/hive/inbox").iterdir())]
assert got == ["Order: see /hive/bot/agent/.runtime/hive/attachments/t-1/review.md"], got
assert mod.as_agent("no paths here") == "no paths here"
print("PASSED: order attachments reach the agent, at paths it can open")
