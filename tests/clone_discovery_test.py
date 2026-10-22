"""The Iter adapter finds an agent's clone of a registered repository by its remote, without config."""
import importlib.util
import json
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def sh(*cmd):
    subprocess.run(cmd, check=True, capture_output=True)


def main():
    tmp = Path(tempfile.mkdtemp())
    for r in ("code", "other"):
        sh("git", "init", "-q", "--bare", str(tmp / f"{r}.git"))
    root = tmp / "agent"
    (root / "work" / "deep").mkdir(parents=True)
    sh("git", "clone", "-q", str(tmp / "code.git"), str(root / "work" / "deep" / "code"))
    sh("git", "clone", "-q", str(tmp / "other.git"), str(root / "work" / "mine"))
    (root / "node_modules").mkdir()
    sh("git", "clone", "-q", str(tmp / "code.git"), str(root / "node_modules" / "code"))   # skipped
    reg = {"version": 1, "repositories": {
        "workspace": {"fetch_urls": [f"file://{tmp}/ws.git"], "local_path": "/x"},
        "code": {"fetch_urls": [f"file://{tmp}/code.git"], "local_path": "/x"},
        "other": {"fetch_urls": [f"{tmp}/other.git/"], "local_path": "/x"},
        "absent": {"fetch_urls": ["https://github.com/someone/absent.git"], "local_path": "/x"}}}
    (tmp / "registry.json").write_text(json.dumps(reg))
    conf = {"actor": "w", "registry": str(tmp / "registry.json"), "hive_url": "x", "hive_id": "x", "key_file": "x",
            "workspace": {"repository": "workspace", "clone": str(tmp / "wsclone"), "branch": "main"}}
    (tmp / "conf.json").write_text(json.dumps(conf))
    spec = importlib.util.spec_from_file_location("hive_channel", HERE / "adapters" / "iter" / "channels" / "hive.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.configure(root, tmp / "conf.json")
    m._dirs()
    h = m.Hive(conf)
    assert h.clone("code") == str(root / "work" / "deep" / "code"), h.repos
    assert h.clone("other") == str(root / "work" / "mine"), h.repos
    run_reg = json.loads((m.RUN / "registry.json").read_text())["repositories"]
    assert run_reg["code"]["local_path"] == str(root / "work" / "deep" / "code"), run_reg["code"]
    assert m.Hive(conf).repos.get("code") == str(root / "work" / "deep" / "code"), "remembered across restarts"
    try:
        h.clone("absent")
        raise AssertionError("absent must fail")
    except m.Failed as e:
        assert "no clone of absent found" in str(e) and "absent.git" in str(e), e
    print("PASSED: clones found by remote (file and path forms, nested, node_modules skipped), registry updated, "
          "remembered across restarts, a missing clone explained")


if __name__ == "__main__":
    main()
