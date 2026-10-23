#!/usr/bin/env python3
"""Put a long-lived agent on the hive, or take it off (--undo): the files the agent itself sees.
Its sidecar (adapters/iter/sidecar.py, with the key) is set up and started separately.

    install-agent.py iter --folder ITER [--prompt prompt.txt] [--undo]
    install-agent.py openclaw --workspace W --config openclaw.json --queue Q --adapter-dir D
                     [--home H] [--map HOST=AGENT] [--hook-token-file F [--agent main]] [--undo]

iter: copies the hive channel and tool into the Iter folder (channels/hive.py, tools/hive_task.py),
writes .hive/config.json = {"daemon": "sidecar"}, and adds the hive rule (adapters/hive-rule.txt) to the
prompt file (default prompt.txt in the folder). --cos-handle names the chief of staff's chat handle in that
rule (the bridge answers only a real mention of its bot), e.g. @hive_cos_bot.

openclaw: copies hive-task (with the channel module it loads) into D, the skill into W/skills/hive/
(naming hive-task's path), adds "hive" to agents.defaults.skills in the config, writes
H/.hive/config.json = {"daemon": "sidecar", "root": Q} (H: the agent's home, default W's parent), and adds
the rule to W/AGENTS.md and a `hive-task inbox` line to W/HEARTBEAT.md.

--hook-token-file F: also enables the gateway's inbound webhook (hooks: /hooks, only --agent allowed, a
token of its own kept in F, mode 600, owned by root) so the agent's sidecar can deliver into it directly
(adapters/openclaw/hook-deliver.py) instead of waiting for the agent to look. The gateway may need a restart.

--map HOST=AGENT: the agent sees HOST's paths under AGENT (a container's mount, e.g.
/opt/proto-hive-runtime=/hive); paths written for the agent are translated.

Every file it changes is backed up once as <file>.pre-hive (kept on --undo), it writes its text between
marker lines so a rerun replaces it, and new files get the owner of the folder they go into.
Run as root.
"""
import argparse
import json
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BEGIN, END = "# --- hive (hive-runtime install-agent.py) ---", "# --- end hive ---"
RULE_TEXT = (HERE / "hive-rule.txt").read_text().strip()   # {cos}: the chief of staff's chat handle (--cos-handle)


def owner_of(p: Path):
    st = p.stat()
    return st.st_uid, st.st_gid


def put(src_text: str, dst: Path, like: Path, mode=0o644):
    missing = [p for p in reversed(dst.parents) if not p.exists()]
    for p in missing:                                 # new folders get like's owner too
        p.mkdir()
        os.chown(p, *owner_of(like))
    dst.write_text(src_text)
    os.chmod(dst, mode)
    os.chown(dst, *owner_of(like))
    print(f"wrote {dst}")


def backup(f: Path):
    b = f.with_name(f.name + ".pre-hive")
    if f.exists() and not b.exists():
        shutil.copy2(f, b)
        os.chown(b, *owner_of(f))


def set_block(f: Path, text: str | None):
    """Put text between the markers at the end of f (or remove the block when text is None)."""
    old = f.read_text() if f.exists() else ""
    if BEGIN in old:
        head, _, rest = old.partition(BEGIN)
        _, _, tail = rest.partition(END)
        old = head.rstrip("\n") + ("\n" if head.strip() else "") + tail.lstrip("\n")
    if text is None:
        new = old
    else:
        new = old.rstrip("\n") + ("\n\n" if old.strip() else "") + f"{BEGIN}\n{text}\n{END}\n"
    if new != (f.read_text() if f.exists() else ""):
        if f.exists():
            backup(f)
        st = f.stat() if f.exists() else None
        f.write_text(new)
        if st:
            os.chown(f, st.st_uid, st.st_gid)
        print(("removed the hive block from " if text is None else "hive block in ") + str(f))


def remove(f: Path):
    if f.exists():
        f.unlink()
        print(f"removed {f}")


def agent_path(p, maps):
    s = str(p)
    for host, agent in maps:
        if s == host or s.startswith(host.rstrip("/") + "/"):
            return agent.rstrip("/") + s[len(host.rstrip("/")):]
    return s


def iter_agent(a):
    f = Path(a.folder).resolve()
    like = f / "channels" if (f / "channels").is_dir() else f
    prompt = Path(a.prompt) if a.prompt else f / "prompt.txt"
    files = {f / "channels" / "hive.py": HERE / "iter" / "channels" / "hive.py",
             f / "tools" / "hive_task.py": HERE / "iter" / "tools" / "hive_task.py"}
    if a.undo:
        for dst in [*files, f / ".hive" / "config.json"]:
            remove(dst)
        set_block(prompt, None)
        return
    for dst, src in files.items():
        put(src.read_text(), dst, like)
    put(json.dumps({"daemon": "sidecar"}) + "\n", f / ".hive" / "config.json", like)
    set_block(prompt, RULE_TEXT.replace("{cos}", a.cos_handle))


def openclaw_agent(a):
    w = Path(a.workspace).resolve()
    maps = [tuple(m.split("=", 1)) for m in a.map or []]
    d = Path(a.adapter_dir)
    home = Path(a.home) if a.home else w.parent
    task_bin = d / "openclaw" / "hive-task"
    conf = Path(a.config)
    if a.undo:
        shutil.rmtree(d, ignore_errors=True)
        print(f"removed {d}")
        shutil.rmtree(w / "skills" / "hive", ignore_errors=True)
        print(f"removed {w / 'skills' / 'hive'}")
        remove(home / ".hive" / "config.json")
        set_block(w / "AGENTS.md", None)
        set_block(w / "HEARTBEAT.md", None)
        skills(conf, add=False)
        if a.hook_token_file:
            hooks(conf, Path(a.hook_token_file), a.agent, add=False)
        return
    put((HERE / "openclaw" / "hive-task").read_text(), task_bin, w, mode=0o755)
    put((HERE / "iter" / "channels" / "hive.py").read_text(), d / "iter" / "channels" / "hive.py", w)
    seen = agent_path(task_bin, maps)
    skill = (HERE / "openclaw" / "SKILL.md").read_text().replace("{cos}", a.cos_handle)
    head, sep, body = skill.partition("\n---\n")
    skill = head + sep + f"\n`hive-task` is `{seen}` (run it by that path).\n" + body
    put(skill, w / "skills" / "hive" / "SKILL.md", w)
    put(json.dumps({"daemon": "sidecar", "root": agent_path(Path(a.queue), maps)}) + "\n",
        home / ".hive" / "config.json", w)
    set_block(w / "AGENTS.md", RULE_TEXT.replace("{cos}", a.cos_handle))
    set_block(w / "HEARTBEAT.md", f"- Run `{seen} inbox`. If it shows a task, a question's answer, a review or a nudge "
                                  "for you, act on it as the hive skill says; otherwise say nothing about it.")
    skills(conf, add=True)
    if a.hook_token_file:
        hooks(conf, Path(a.hook_token_file), a.agent, add=True)


def hooks(conf: Path, token_file: Path, agent: str, add: bool):
    """The gateway's inbound webhook for the sidecar: enabled with a token of its own (kept in token_file),
    for one agent only, no caller-chosen session keys. Removed again only if it's still ours."""
    import secrets
    c = json.loads(conf.read_text())
    cur = c.get("hooks") or {}
    token = token_file.read_text().strip() if token_file.is_file() else None
    if not add:
        if token and cur.get("token") == token:
            del c["hooks"]
            write_config(conf, c)
            print(f"removed the hive webhook from {conf}")
        if token_file.is_file():
            token_file.unlink()
        return
    if cur.get("enabled") and cur.get("token") and cur.get("token") != token:
        sys.exit(f"{conf} already has a webhook with another token: not touching it")
    if not token:
        token = secrets.token_urlsafe(32)
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(token + "\n")
        os.chmod(token_file, 0o600)
    want = {"enabled": True, "token": token, "path": "/hooks", "allowedAgentIds": [agent], "allowRequestSessionKey": False}
    if cur != want:
        c["hooks"] = want
        write_config(conf, c)
        print(f"enabled the gateway webhook for agent {agent} in {conf} (token in {token_file})")


def write_config(conf: Path, c: dict):
    backup(conf)
    st = conf.stat()
    conf.write_text(json.dumps(c, indent=2) + "\n")
    os.chown(conf, st.st_uid, st.st_gid)
    os.chmod(conf, st.st_mode & 0o777)


def skills(conf: Path, add: bool):
    c = json.loads(conf.read_text())
    d = c.setdefault("agents", {}).setdefault("defaults", {})
    have = d.get("skills")
    if have is None:
        print(f"{conf}: no skills allowlist, so every skill is allowed; nothing to change")
        return
    want = ([s for s in have if s != "hive"] + ["hive"]) if add else [s for s in have if s != "hive"]
    if want == have:
        return
    d["skills"] = want
    write_config(conf, c)
    print(f"{'added' if add else 'removed'} skill hive {'to' if add else 'from'} agents.defaults.skills in {conf}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="kind", required=True)
    i = sub.add_parser("iter")
    i.add_argument("--folder", required=True)
    i.add_argument("--prompt")
    i.add_argument("--undo", action="store_true")
    i.add_argument("--cos-handle", default="@cos", help="the chief of staff's chat handle, e.g. @hive_cos_bot")
    o = sub.add_parser("openclaw")
    for x in ("--workspace", "--config", "--queue", "--adapter-dir"):
        o.add_argument(x, required=True)
    o.add_argument("--home")
    o.add_argument("--map", action="append")
    o.add_argument("--hook-token-file")
    o.add_argument("--cos-handle", default="@cos", help="the chief of staff's chat handle, e.g. @hive_cos_bot")
    o.add_argument("--agent", default="main")
    o.add_argument("--undo", action="store_true")
    a = ap.parse_args()
    if os.geteuid() != 0:
        sys.exit("run as root (it sets owners)")
    (iter_agent if a.kind == "iter" else openclaw_agent)(a)


if __name__ == "__main__":
    main()
