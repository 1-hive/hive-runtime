# Iter adapter: long-lived agents on a hive record

For hives whose agents run continuously ([Iter](https://github.com/patham9/iter) loops), instead of being launched per task. The agent keeps its process and memory. Its work comes from the record, never from chat, and everything it does on the record is signed with its own key.

- **`channels/hive.py`** (copy to the agent's `channels/`):
  - **What it delivers** on the `[hive]` channel: each task assigned to the agent, with its order; an answer to its question; a review of its result; a result for it to review (if it's a reviewer); and the outcome of each action it asked for. When the channel starts, it first reports everything already waiting on the agent.
  - **`send` on this channel** posts a short progress report on the agent's one open task.
- **`tools/hive_task.py`** (copy to the agent's `tools/`): `hive_task(action, task, text, extra)`, with these actions:
  - `propose`: a task someone asked for in chat (text: a title line, then what is wanted; extra: where it was asked, `telegram:<chat>/<message>`). The sidecar commits the order under `proposals/` in the workspace and submits `proposal.submitted` (a complete `task.created`, for the chief of staff; `cause` = that message), then tells the agent when the chief of staff approves or rejects it. Works from any chat. A hive whose tasks need a goal sets `propose_goal` in the config;
  - `accept`: when it starts;
  - `progress`, `checkpoint`;
  - `block`: a question, with `extra` set to `decision`, `information`, `access` or `external`;
  - `unblock`;
  - `result`;
  - `review`: with `extra` set to `passed`, `failed` or `needs_information`.

Iter gives a channel or tool call 5 seconds. So the channel and the tool only queue files, and a small daemon does the rest: it reads the record, commits and pushes reports to the workspace, mints pins, and emits signed events with `hive` and `hive-pin`, citing what each one responds to (`cause`, record amendment A5: an acceptance cites the assignment, a result the review it answers, a review the result it judges). The daemon runs in one of two ways:

- **Spawned:** the channel starts it on first use, inside the agent, from the agent's own `.hive/config.json`.
- **Sidecar** (for agents in a container without git, or whose key must stay out of their reach): the agent's `.hive/config.json` says only `{"daemon": "sidecar"}`, and the daemon runs outside, as the agent's uid, started as `sidecar.py --root <the agent's folder> --config <config.json kept outside that folder>`. The two sides share only the queue files under `<root>/.runtime/hive`. The sidecar needs Python 3.11 or newer, git, `hive` and `hive-pin`. A systemd unit per agent:

  ```ini
  [Service]
  User=hive-protomega2
  ExecStart=/opt/hive/uv run --project /opt/hive/hive-record python /opt/hive/hive-runtime/adapters/iter/sidecar.py --root /opt/proto-hive-runtime/protomega2/iter-port-omega/repos --config /var/lib/hive-sidecars/protomega2/config.json
  Restart=always
  ```

**OpenClaw agents (`adapters/openclaw/hive-task`)** use the same daemon. An OpenClaw agent only acts when something starts a turn, so its sidecar needs `deliver`: `adapters/openclaw/hook-deliver.py`, which posts each message to the gateway's inbound webhook (`/hooks/agent`, enabled by `install-agent.py openclaw --hook-token-file`). Without it, work waits until the agent happens to run `hive-task inbox` (the heartbeat line `install-agent.py` adds is only a fallback). Never deliver through chat: a control signal isn't conversation. See `docs/long-lived-agents.md`. By default `hive-task` does the record and git work itself, so the agent holds its key. In sidecar mode its config is only `{"daemon": "sidecar", "root": "<a queue folder>"}`: each command queues the action and waits for the sidecar's answer, and `inbox` prints what the sidecar queued. The sidecar runs as above with `--root` at that folder, as the sidecar sees it (on vm2, the container's `/hive/<bot>/…` is the host's `/opt/proto-hive-runtime/<bot>/…`). Copy `hive-task` with this folder's `channels/hive.py` beside it as `../iter/channels/hive.py`. The agent's container then needs only Python, no git, `hive` or key.

**Sidecar under another user than the agent** (pop-os, where agents share one user): the queue folder must be writable by both. Make it, and its `inbox`, `outbox`, `tasks` and `pins` subfolders, group-owned by a group both users are in, mode `2770`, and give the sidecar's unit `UMask=0007`. For the declaration, the sidecar user must be able to read the agent's folder, and git must trust it: `git config --global --add safe.directory <the folder>` as that user.

## Configuration: `.hive/config.json` in the agent's folder

```json
{
  "actor": "worker.protomegabot2",
  "hive_url": "http://127.0.0.1:8470",
  "hive_id": "<hive id>",
  "key_file": "/path/to/this agent's record key",
  "registry": "/path/to/the hive's registry.json",
  "workspace": {"repository": "workspace", "clone": "/path/to/the agent's workspace clone", "branch": "main"},
  "repos": {"hive-appliance": "/path/to/the agent's clone of it"},
  "poll_seconds": 30,
  "hive_bin": "hive",
  "hive_pin_bin": "hive-pin"
}
```

- **Key:** `key_file` must be readable only by the agent's own OS user (record SPEC §6.6). Never share one key between agents.
- **Workspace:** reports, questions and reviews are committed to the workspace clone under `projects/<project>/reports/` and pushed to `branch`. The agent needs push access there.
- **Code repos:** `repos` lists the code repositories the agent may change, each with the agent's own clone. It can stay empty: for a repository the order names but `repos` doesn't, the daemon looks under `repo_search` (a list of folders; default the agent's folder; 4 levels deep, skipping `node_modules`, caches and virtualenvs) for a git folder whose remote is one of the repository's fetch URLs in the registry (https, ssh and scp-like forms, with or without `.git`, match), and remembers it in `.runtime/hive/repos.json`. When the task's order names repositories (`ext.repos`, 1-hive profile), `accept` records each one's current commit as the base, and `result` pins the pushed `HEAD` and that base. A result whose `HEAD` isn't on a remote branch is refused: push first (the daemon asks the clone's remotes, then the registry's fetch URLs, so a sidecar without the agent's ssh key can still check a public repository). Work starts from the repository's default branch, unless the operators set another for this agent in `base_branches` ({repository: branch}; e.g. a bot whose own code lives on a branch with no history in common with the default one); never from one the agent picks, which could hide part of its change from the review. With no base recorded at accept (the clone came later), `result` takes that branch's tip. On the core profile, a result carries only its report.
- **Declaration** (`declare_seconds`, default 600; 0 turns it off): the agent's code may change under it (Iter agents modify themselves), and the record's SPEC §6.2 asks agents to re-declare when it does. Every `declare_seconds` the daemon computes the agent's code state, and if it changed, emits `actor.declared` with it in the declaration's `harness` line (model and sandbox stay as declared): `iter <branch>@<commit>[+<hash of uncommitted changes>]; hive adapter <hash>[; <name> <hash>…]`. Uncommitted changes are tracked files' changes plus untracked code files (`.py`, `.metta`, `.sh`, `.js`, `.ts`) in the agent's folder, minus `.runtime/`, `.hive/` and `declare_exclude`. `declare_files` ({name: path in the agent's folder}) adds a hash of each such file, e.g. a memory system's flags. `harness` sets the first word (default `iter`); a daemon whose folder holds no `channels/hive.py` (hive-task's queue folder) declares nothing unless it is set.
- **Registry:** the daemon writes its own copy of the hive's registry, pointing each repository's `local_path` at the agent's clones, so pins resolve the agent's commits.

## Beacons: what the agent is doing now

With `"beacon": {"url": ...}` in its config, the daemon (and the reviewer runner, `adapters/reviewer/`) posts a beacon to that URL every `every` seconds (default 30), on a thread of its own, so a slow delivery never stops it: the hive's Floor (hive-bridge) shows from these what each agent is doing, and an agent whose beacons stop as offline. A beacon is built from the agent's own files only, is never authoritative, and **never carries the text of a prompt, a message or a reply**: only times, counts, model names and chat and message ids.

| Config key (under `beacon`) | What it adds to the beacon |
|---|---|
| `url` | where to post (required; without it, no beacons) |
| `every` | seconds between beacons (default 30) |
| `iter_channel` | an Iter outer channel folder (`inbox/`, `processing/`, `completed/`): `requests` (each claimed request's `chat_id`, `message_id` and since when), `queued` (waiting in `inbox/`), `last_answer` and `answers` (completed in the last 24 h) |
| `route_hook` | the hive-route hook log (default `route-hook.jsonl` in the agent's folder, if it exists): `episodes`, `[start, end or null, model]` for the last 24 h |
| `watch` | files whose last change is `last_step` (an Iter agent's `experience.json` is always watched) |
| `openclaw_sessions` | an OpenClaw agent's sessions folder: `last_step` (the newest transcript) and `chat` (the chat of the session updated last, from `sessions.json`'s keys) |

Always: `v` (1), `actor`, `kind` (`iter`, `openclaw`, `reviewer` or `agent`), `at`, `started`, `problem` (e.g. a folder it can't read, or the reviewer's last failed run), and `pending_actions` (the agent's actions waiting in the queue). The reviewer runner adds `reviewing` (`{task, since}` while a review runs). Times are Unix seconds; a file's time is its last change (mtime or ctime, whichever is later, so a request counts from when it was claimed).

**Signed** with the agent's record key, the way the record signs writes but under another prefix, so a beacon can never be replayed as a record write: the body is canonical JSON (sorted keys, no spaces), and the headers are `X-Hive-Key` (the public key), `X-Hive-Signed-At` (UTC, `YYYY-MM-DDTHH:MM:SSZ`) and `X-Hive-Signature`, the ed25519 signature of `hive-beacon1\n<hive id>\n<signed at>\n<sha256 hex of the body>`. The receiver checks the key belongs to an active actor and the time is recent. Test: `tests/beacon_test.py`.

**What a receiver does** (hive-bridge's Floor, `POST /api/beacon`; another receiver should do the same): refuse a body over 128 kB (413) or one that isn't JSON (400); refuse a `X-Hive-Signed-At` more than 5 minutes off its clock (403), a bad signature (403), or a key that isn't an active actor's on the record, or whose actor isn't the body's `actor` (403); otherwise keep the beacon as that actor's latest and answer 204. An actor whose last beacon is older than 150 s is shown as offline, and one with a request in progress but no step for 15 minutes as maybe stuck. Beacons are display only: nothing on the record, in a task's state or in any decision reads them.

State lives in `.runtime/hive/` in the agent's folder: the queues, the last state seen, minted pins, and a log (`log.txt`).

To turn them on in an existing config: `adapters/set-beacon.py <config.json> --url https://<floor>/api/beacon [--iter-channel DIR] [--openclaw-sessions DIR]` (keeps the file's owner and mode; `--off` removes them), then restart the sidecar or runner.

## Conformance: does a given Iter work with the adapter?

Iter agents differ (forks, self-modification), and the adapter must keep working under each. `conformance.py` checks one agent's folder the way that agent calls the adapter: through its own `iter.py --invoke` worker, under its own timeouts and tool limit, in a scratch copy (sidecar mode, so the agent's live queue and the record are never touched).

```
python adapters/iter/conformance.py --folder <the agent's Iter folder> --python <the agent's interpreter> [--prompt FILE] [--json]
```

Run it where the agent runs, with the agent's interpreter and the environment it imports `iter.py` with (`--env KEY=VALUE`, repeatable). For a bot in a container, as its uid, e.g. on vm2: copy `conformance.py`, `channels/hive.py` and `tools/hive_task.py` into a folder in the container's `/tmp`, then `docker exec -u 11003 proto-hive /usr/bin/python3 <that folder>/conformance.py --folder /hive/protomega/iter-port-omega/repos --python /usr/bin/python3 --env PYTHONPATH=/hive/protomega/iter-port-omega/vendor --env HOME=/hive/protomega/iter-port-omega-home`. If Iter's worker can't start, the failure names its last error line.

It checks that the channel, tool and config are installed; that Iter loads `hive_task` (it loads only the first `MAX_TOOLS` tools, by name) and reads its parameters and whole description; that a queued message comes out of `receive()` once and the tool queues an action, both within Iter's limits; that Iter's channel probe (request binding) answers for the hive channel; whether the agent's own `send` tool reaches the hive channel, unbound and while a chat request is bound to the turn; whether the installed files are this adapter's; and that the hive rule is in the prompt. `FAIL` breaks hive work (exit 1); `WARN` works with a limit worth knowing. Run it after installing, after an Iter upgrade or merge, and from time to time on agents that modify themselves. Test: `tests/iter_conformance_test.py`.

First run (2026-10-23) on the three Iter variants of the proto-hive (ProtoCosmo2's lineage from GitHub, Protomega's and Protomega2's from vm2): no failures. It found `hive_task`'s description cut at Iter's 500 characters (fixed in v0.5.10) and that VM2's `send` tool refuses the hive channel while a turn is bound to a chat request (`hive_task` progress still works), and noted both vm2 bots carry an older `channels/hive.py`. On the live vm2 bots after reinstalling (same day, in their container, as their uids): all checks pass, with only the bound-`send` warning.

## Test

`tests/iter_adapter_e2e.py` runs a worker and a reviewer on a scratch record, the way `iter.py` calls channels and tools (a new process per call, from the agent's folder). The worker runs in sidecar mode and the reviewer in spawned mode. The task goes through:
1. assigned, accepted;
2. progress, through the tool and through `send`, and a nudge from the supervisor, delivered as a message;
3. blocked, answered, unblocked;
4. a result refused until pushed, then a result with code and base pins;
5. a failed review, a new result, a passing review;
6. closed.

```
uv run --project ~/repos/hive-record python tests/iter_adapter_e2e.py
```
