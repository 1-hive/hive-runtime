#!/usr/bin/env python3
"""The hive adapter's daemon, run outside the agent: on the host, as the agent's uid, next to the
agent's folder but with the record key where the agent itself can't read it.

    sidecar.py --root <the agent's Iter folder> --config <config.json, outside the agent's folder>

The agent's own .hive/config.json says only {"daemon": "sidecar"}; its hive channel and hive_task
tool then only read and write the queue files under <root>/.runtime/hive, and this process does
everything else (record reads and signed events, git, pins). One sidecar per agent; a systemd unit
is the usual way to run it (README.md).
"""
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> None:
    args = sys.argv[1:]
    if "--root" not in args or "--config" not in args:
        sys.exit(__doc__)
    spec = importlib.util.spec_from_file_location("hive_channel", HERE / "channels" / "hive.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.configure(args[args.index("--root") + 1], args[args.index("--config") + 1])
    mod.daemon()


if __name__ == "__main__":
    main()
