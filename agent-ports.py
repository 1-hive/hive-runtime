#!/usr/bin/env python3
"""1-hive agent ports: the only host services an agent container can reach.

Agent containers have no route to the host's loopback (slirp4netns without
allow_host_loopback), so the record's Postgres and every other local service stay out
of reach. This relays three Unix sockets, which launch-task.sh mounts read-only into
each container, to the services agents need:

    record.sock  -> 127.0.0.1:8470  (the record's gateway)
    gateway.sock -> 127.0.0.1:4000  (the model gateway)
    ssh.sock     -> 127.0.0.1:22    (sshd: git, as the actor's key allows)

Inside, the agent image's hive-entry listens on those same ports on the container's
loopback and relays them to the sockets. Sockets are in ~/work/1hive/.ports, which
only the group `hive` may enter (deploy/agent-users.sh).

    agent-ports.py [--dir ~/work/1hive/.ports]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
from pathlib import Path

PORTS = {"record.sock": 8470, "gateway.sock": 4000, "ssh.sock": 22}


async def pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
    try:
        while data := await r.read(65536):
            w.write(data)
            await w.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            w.close()
        except Exception:
            pass


def handler(port: int):
    async def handle(cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
        try:
            ur, uw = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            cw.close()
            return
        await asyncio.gather(pipe(cr, uw), pipe(ur, cw))
    return handle


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dir", default=str(Path.home() / "work" / "1hive" / ".ports"))
    a = ap.parse_args()
    d = Path(a.dir)
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o750)
    # Agent users reach the sockets through the group `hive`: traversal only.
    subprocess.run(["setfacl", "-m", "g:hive:x", str(d)], check=False)
    servers = []
    for name, port in PORTS.items():
        path = d / name
        path.unlink(missing_ok=True)
        servers.append(await asyncio.start_unix_server(handler(port), path=str(path)))
        os.chmod(path, 0o666)   # reachable only through the directory's ACL
    print(f"agent ports up in {d}: {', '.join(f'{n}->{p}' for n, p in PORTS.items())}", flush=True)
    await asyncio.gather(*(s.serve_forever() for s in servers))


if __name__ == "__main__":
    asyncio.run(main())
