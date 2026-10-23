#!/usr/bin/env python3
"""Turn beacons on (or off) in a sidecar's or reviewer runner's config, in one short command.

    set-beacon.py <config.json> --url https://<floor>/api/beacon [--iter-channel DIR] [--openclaw-sessions DIR] [--watch FILE]...
    set-beacon.py <config.json> --off

Keeps the file's owner and mode. Restart the sidecar or runner afterwards. The keys are adapters/iter/README.md's
"Beacons" section; a folder the sidecar's user can't read shows on the Floor as the beacon's problem.
"""
import argparse
import json
import os
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("config")
    ap.add_argument("--url")
    ap.add_argument("--iter-channel")
    ap.add_argument("--openclaw-sessions")
    ap.add_argument("--route-hook")
    ap.add_argument("--watch", action="append", default=[])
    ap.add_argument("--off", action="store_true")
    a = ap.parse_args()
    f = Path(a.config)
    st = f.stat()
    c = json.loads(f.read_text())
    if a.off:
        c.pop("beacon", None)
    else:
        if not a.url:
            ap.error("--url is required (or --off)")
        b = {"url": a.url}
        for k, v in (("iter_channel", a.iter_channel), ("openclaw_sessions", a.openclaw_sessions), ("route_hook", a.route_hook)):
            if v:
                if not Path(v).is_dir() and k != "route_hook":
                    print(f"note: {v} isn't a folder here (or isn't readable by you)")
                b[k] = v
        if a.watch:
            b["watch"] = a.watch
        c["beacon"] = b
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(c, indent=1) + "\n")
    os.chown(tmp, st.st_uid, st.st_gid)
    os.chmod(tmp, st.st_mode & 0o7777)
    tmp.replace(f)
    print(f"{f}: beacon {'off' if a.off else json.dumps(c['beacon'])}")


if __name__ == "__main__":
    main()
