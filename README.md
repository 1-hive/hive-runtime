# hive-runtime

How agents do work on a hive record: launching workers, dispatching independent reviews, supervising stuck work, cleaning up, and the chief of staff's tools. It's the operating loop from 1-hive's plan (Phase E), a minimal form of One Hive release R6.

**Status (2026-10-19):** extracted from [1-hive](https://github.com/1-hive/1-hive) with its history, and not yet generalized. The code still has 1-hive's paths and its per-task container setup. Next: configuration per hive, and an adapter for long-lived agents (an Iter `hive` channel) next to the per-task launcher.

- **Launching work:** `launch-task.sh` (one attempt per assigned task, in the agent's own rootless container, routed by hive-route), `watch-task.sh`, `result-refs.sh`, `agent/` (the agent image), `agent-users.sh` (one OS user per agent).
- **The loop:** `dispatcher.py` (launches workers and independent reviewers), `supervisor.py` (nudges, restarts, reassigns, escalates), `sweeper.py` (removes task folders the record no longer needs).
- **The chief of staff:** `hive-mcp.py` (an MCP server over the record), `cos.sh`, `docs/chief-of-staff.md` (its role).
- **Isolation:** `agent-ports.py` (the only host services containers reach), `git/` (per-actor git over SSH, with a pre-receive hook that keeps pinned commits reachable).
- **The operator:** `op`, short commands that sign with the operator's key.
- `docs/worker-contract.md`: what a worker must do on the record.

Related: [hive-record](https://github.com/1-hive/hive-record) (R1 + R2), [hive-bridge](https://github.com/1-hive/hive-bridge) (where humans meet the hive), [hive-route](https://github.com/1-hive/hive-route) (R8), [hive-pin](https://github.com/1-hive/hive-pin) (R0).
