#!/usr/bin/env python3
"""Deliver one message from the hive straight into an OpenClaw agent: a turn through the gateway's
inbound webhook (POST <url>, i.e. /hooks/agent), never through chat. A sidecar's "deliver" command.

    hook-deliver.py --url http://127.0.0.1:18789/hooks/agent --token-file F [--agent main] TEXT

The webhook must be enabled in the gateway's config (install-agent.py openclaw --hook-token-file F).
The reply isn't posted anywhere ("deliver": false): the agent acts through hive-task. Exits non-zero
when the gateway doesn't admit the turn, so the sidecar keeps the message in the queue for `hive-task inbox`.
"""
import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid

PREFIX = ("From the hive record, through your sidecar. Act on it with `hive-task`, as your hive skill says "
          "(a form like hive_task(action='accept', task='t1', ...) below means `hive-task accept t1 ...`).\n\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True)
    ap.add_argument("--token-file", required=True)
    ap.add_argument("--agent", default="main")
    ap.add_argument("--timeout", type=float, default=30)
    ap.add_argument("text")
    a = ap.parse_args()
    token = open(a.token_file).read().strip()
    body = json.dumps({"message": PREFIX + a.text, "name": "hive", "agentId": a.agent, "deliver": False}).encode()
    req = urllib.request.Request(a.url, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json", "Idempotency-Key": str(uuid.uuid4())})
    try:
        with urllib.request.urlopen(req, timeout=a.timeout) as r:
            out = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        sys.exit(f"hook-deliver: HTTP {e.code}: {e.read()[:300].decode(errors='replace')}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        sys.exit(f"hook-deliver: {e}")
    if not out.get("ok"):
        sys.exit(f"hook-deliver: not admitted: {json.dumps(out)[:300]}")
    print(f"admitted: run {out.get('runId')}")


if __name__ == "__main__":
    main()
