"""adapters/install-agent.py on temporary folders: install, rerun (no duplicates), undo; iter and openclaw."""
import argparse
import importlib.util
import json
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def main():
    spec = importlib.util.spec_from_file_location("ia", HERE / "adapters" / "install-agent.py")
    ia = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ia)
    tmp = Path(tempfile.mkdtemp())

    it = tmp / "iter"
    (it / "channels").mkdir(parents=True)
    (it / "tools").mkdir()
    (it / "prompt.txt").write_text("You are a bot.\n")
    a = argparse.Namespace(folder=str(it), prompt=None, undo=False, cos_handle="@hive_cos_bot")
    ia.iter_agent(a)
    ia.iter_agent(a)
    p = (it / "prompt.txt").read_text()
    assert p.count(ia.BEGIN) == 1 and "Never push to main" in p and p.startswith("You are a bot."), p
    assert "(@hive_cos_bot)" in p and "{cos}" not in p and "@cos " not in p, p
    assert (it / "channels" / "hive.py").is_file() and (it / "tools" / "hive_task.py").is_file()
    assert json.loads((it / ".hive" / "config.json").read_text()) == {"daemon": "sidecar"}
    assert (it / "prompt.txt.pre-hive").read_text() == "You are a bot.\n"
    ia.iter_agent(argparse.Namespace(folder=str(it), prompt=None, undo=True, cos_handle="@hive_cos_bot"))
    assert (it / "prompt.txt").read_text() == "You are a bot.\n", (it / "prompt.txt").read_text()
    assert not (it / "channels" / "hive.py").exists() and not (it / ".hive" / "config.json").exists()

    host = tmp / "host"
    w = host / "bot" / "workspace"
    (w / "skills").mkdir(parents=True)
    (w / "AGENTS.md").write_text("# Agents\n")
    (w / "HEARTBEAT.md").write_text("# Heartbeat\n")
    conf = host / "bot" / "config" / "openclaw.json"
    conf.parent.mkdir()
    conf.write_text(json.dumps({"agents": {"defaults": {"skills": ["github"]}}, "x": 1}))
    o = argparse.Namespace(workspace=str(w), config=str(conf), queue=str(host / "bot" / ".hive" / "queue"),
                           adapter_dir=str(host / "bot" / "hive-adapter"), home=None, map=[f"{host}=/hive"], undo=False,
                           hook_token_file=str(tmp / "root" / "hook-token"), agent="main", cos_handle="@hive_cos_bot")
    ia.openclaw_agent(o)
    ia.openclaw_agent(o)
    c = json.loads(conf.read_text())
    assert c["agents"]["defaults"]["skills"] == ["github", "hive"] and c["x"] == 1, c
    sk = (w / "skills" / "hive" / "SKILL.md").read_text()
    assert "`/hive/bot/hive-adapter/openclaw/hive-task`" in sk and sk.startswith("---"), sk[:300]
    assert "(@hive_cos_bot) approves it" in sk and "{cos}" not in sk and "hive-task propose" in sk
    assert "(@hive_cos_bot)" in (w / "AGENTS.md").read_text()
    assert json.loads((host / "bot" / ".hive" / "config.json").read_text()) == {"daemon": "sidecar", "root": "/hive/bot/.hive/queue"}
    hb = (w / "HEARTBEAT.md").read_text()
    assert hb.count("/hive/bot/hive-adapter/openclaw/hive-task inbox") == 1, hb
    assert (w / "AGENTS.md").read_text().count("Never push to main") == 1
    assert (host / "bot" / "hive-adapter" / "iter" / "channels" / "hive.py").is_file()
    tok = (tmp / "root" / "hook-token").read_text().strip()
    hk = c["hooks"]
    assert hk == {"enabled": True, "token": tok, "path": "/hooks", "allowedAgentIds": ["main"],
                  "allowRequestSessionKey": False} and len(tok) > 30, hk
    assert oct((tmp / "root" / "hook-token").stat().st_mode & 0o777) == "0o600"
    o.undo = True
    ia.openclaw_agent(o)
    assert json.loads(conf.read_text())["agents"]["defaults"]["skills"] == ["github"]
    assert "hooks" not in json.loads(conf.read_text()) and not (tmp / "root" / "hook-token").exists()
    assert (w / "AGENTS.md").read_text() == "# Agents\n" and (w / "HEARTBEAT.md").read_text() == "# Heartbeat\n"
    assert not (w / "skills" / "hive").exists() and not (host / "bot" / "hive-adapter").exists()
    print("PASSED: iter and openclaw: install, rerun without duplicates (same webhook token), paths translated for the agent, webhook enabled for one agent with a 600 token file, undo restores")


if __name__ == "__main__":
    main()
