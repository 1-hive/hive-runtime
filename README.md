# hive-runtime

How agents do work on a hive record: launching workers, dispatching independent reviews, supervising stuck work, cleaning up, and the chief of staff's tools. It's the operating loop from 1-hive's plan (Phase E), a minimal form of One Hive release R6.

**Status (2026-10-19):** extracted from [1-hive](https://github.com/1-hive/1-hive) with its history. Paths and ids are configuration (below); 1-hive runs it with the defaults. Long-lived agents (Iter) work through [`adapters/iter`](adapters/iter/README.md), next to the per-task launcher.

- **Launching work:** `launch-task.sh` (one attempt per assigned task, in the agent's own rootless container, routed by hive-route), `watch-task.sh`, `result-refs.sh`, `agent/` (the agent image), `agent-users.sh` (one OS user per agent).
- **The loop:** `dispatcher.py` (launches workers and independent reviewers), `supervisor.py` (nudges, restarts, reassigns, escalates), `sweeper.py` (removes task folders the record no longer needs).
- **The chief of staff:** `hive-mcp.py` (an MCP server over the record), `docs/chief-of-staff.md` (its role on 1-hive) and `docs/chief-of-staff-proto-hive.md` (on the proto-hive: no goals, a work group, long-lived agents), `cos.sh` (its container; `COS_ROLE_DOC` picks the role doc: rootless Podman with keys as secrets, or, with `HIVE_CONTAINER_RUNTIME=docker`, plain Docker with the keys bind-mounted read-only; the Docker path isn't exercised on 1-hive).
- **Isolation:** `agent-ports.py` (the only host services containers reach), `git/` (per-actor git over SSH, with a pre-receive hook that keeps pinned commits reachable).
- **The operator:** `op`, short commands that sign with the operator's key.
- `docs/worker-contract.md`: what a worker must do on the record.
- **`main-watch.py`:** where agents could push to `main` but by agreement don't (they push a branch and a human merges the pull request), flags commits that reach `main` without a passed review on the record, after the fact; from a timer, alerts through e.g. hive-bridge's `telegram-say.py`. `--registry R --under DIR` watches every registered repository cloned under DIR, on its default branch; `--also NAME:BRANCH` adds another branch of one of them (a line agents track besides the default, e.g. `iter:protomega`). Test: `tests/main_watch_test.py`.
- **[`docs/long-lived-agents.md`](docs/long-lived-agents.md):** how to put a hive of long-lived agents (chat bots) on the record: sidecars, delivery per kind of agent, keys when agents share a user, self-modification, repositories, pitfalls.
- **`publisher.py`:** for staged repositories (the registry lists a hive copy first): results wait in the hive copy until their review passes, then this pushes the reviewed commit to the repository's home as `hive/<task>` (after a scan for secrets and `--deny` patterns) and posts the pull-request link; it also mirrors each home into its copy. From a timer. `docs/long-lived-agents.md`, "Review before publishing". Test: `tests/publisher_test.py`.
- **`adapters/install-agent.py`:** puts a long-lived agent on the hive, or takes it off (`--undo`): the hive channel and tool (Iter) or `hive-task`, its skill, the allowlist entry and heartbeat line (OpenClaw), and the hive rule (`adapters/hive-rule.txt`) in its prompt; idempotent, with a `.pre-hive` backup of each file it changes. Test: `tests/install_agent_test.py`.
- **`adapters/openclaw/hook-deliver.py`:** an OpenClaw agent's sidecar delivers each message from the record straight into the agent, as a turn through the gateway's inbound webhook (`/hooks/agent`, its own token, one agent only, nothing posted to chat), instead of waiting for the agent to look; `install-agent.py openclaw --hook-token-file F` enables the webhook. Test: `tests/hook_deliver_test.py`.
- **Long-lived agents:** `adapters/iter/`, a `hive` channel and a `hive_task` tool for Iter agents (spawned daemon, or a sidecar outside the agent); `adapters/openclaw/`, the `hive-task` command and a skill for agents with a shell; `adapters/reviewer/`, a runner for a reviewer behind a command (Astra); `adapters/iter/time_split.py`, how much of an agent's model time went to record tasks. Sidecars and the reviewer runner post signed **beacons** (what the agent is doing now, from its own files; never message text) to hive-bridge's Floor: `adapters/iter/README.md`, test `tests/beacon_test.py`. All tested end to end on a scratch record: `tests/iter_adapter_e2e.py`. `adapters/iter/conformance.py` checks that a given Iter agent (any variant) works with the adapter, through its own `--invoke` worker and limits; test `tests/iter_conformance_test.py`.

Related: [hive-record](https://github.com/1-hive/hive-record) (R1 + R2), [hive-bridge](https://github.com/1-hive/hive-bridge) (where humans meet the hive), [hive-route](https://github.com/1-hive/hive-route) (R8), [hive-pin](https://github.com/1-hive/hive-pin) (R0).

## Configuration

Every tool reads the same environment variables (`paths.sh` for the shell tools, `hive_paths.py` for the Python ones). The defaults are 1-hive's layout, so 1-hive sets none of them; another hive sets the ones that differ, e.g. in its systemd units.

| Variable | Default | What it is |
|---|---|---|
| `HIVE_ID` | `1-hive` | The hive's id on its record |
| `HIVE_GATEWAY_PORT` | `8470` | The record gateway's port on the host. Inside an agent container the record is always at `127.0.0.1:8470` (`agent/hive-entry`) |
| `HIVE_URL` | `http://127.0.0.1:$HIVE_GATEWAY_PORT` | The record gateway, from the host |
| `HIVE_CONFIG_DIR` | `~/.config/hive` | Keys and secrets: `agents/<actor>.key`, `operator.key`, `claude-oauth-token`, `gateway.env`, `route-overrides.json` |
| `HIVE_WORK_DIR` | `~/work/1hive` | Task folders, and the tools' state (`.ports`, `.route`, `.supervisor`, `.sweeper`, `cos`) |
| `HIVE_REPOS_DIR` | `~/repos` | The hive's git remotes, `<name>.git` |
| `HIVE_DEPLOY_DIR` | `$HIVE_REPOS_DIR/1-hive/deploy` | The hive's declared configuration: `registry.json`, `route-table.yaml`, `route-sources.yaml`, `review-allowlist.json`, `runtime` |
| `HIVE_WORKSPACE_REPO` | `$HIVE_REPOS_DIR/hive-workspace.git` | The workspace remote (orders, reports, questions) |
| `HIVE_ROUTE_DIR` | `$HIVE_REPOS_DIR/hive-route` | The router's checkout |
| `HIVE_DEFAULT_REPO` | `mtg-player` | The code repository every task folder holds; others come as `EXTRA_REPOS` |
| `HIVE_LONG_LIVED_ACTORS` | (none) | Comma-separated actor-id patterns (`worker.*,reviewer.astra`) that are never launched or restarted: their sidecar or runner takes work from the record. The dispatcher only assigns reviews to them; the supervisor nudges, then escalates to the chief of staff, and escalates a result whose review waits over `HIVE_REVIEW_WAIT_SECONDS` (7200) |
| `HIVE_REVIEWERS` | `reviewer.codex.1,reviewer.claude.1` | The reviewers the dispatcher assigns, in order of preference |

Two need care, because they're read outside the operator's session:
- `git/hive-git-shell` runs as sshd's forced command, with a clean environment. A hive whose repositories aren't in `~/repos` writes `HIVE_REPOS_DIR=…` into the forced command.
- `agent-users.sh` runs under `sudo`: pass `HIVE_WORK_DIR` on its command line when it isn't the default.
