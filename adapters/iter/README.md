# Iter adapter: long-lived agents on a hive record

For hives whose agents run continuously ([Iter](https://github.com/patham9/iter) loops), instead of being launched per task. The agent keeps its process and memory. Its work comes from the record, never from chat, and everything it does on the record is signed with its own key.

- **`channels/hive.py`** (copy to the agent's `channels/`):
  - **What it delivers** on the `[hive]` channel: each task assigned to the agent, with its order; an answer to its question; a review of its result; a result for it to review (if it's a reviewer); and the outcome of each action it asked for. When the channel starts, it first reports everything already waiting on the agent.
  - **`send` on this channel** posts a short progress report on the agent's one open task.
- **`tools/hive_task.py`** (copy to the agent's `tools/`): `hive_task(action, task, text, extra)`, with these actions:
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
- **Code repos:** `repos` lists the code repositories the agent may change, each with the agent's own clone. When the task's order names repositories (`ext.repos`, 1-hive profile), `accept` records each one's current commit as the base, and `result` pins the pushed `HEAD` and that base. A result whose `HEAD` isn't on a remote branch is refused: push first. On the core profile, a result carries only its report.
- **Registry:** the daemon writes its own copy of the hive's registry, pointing each repository's `local_path` at the agent's clones, so pins resolve the agent's commits.

State lives in `.runtime/hive/` in the agent's folder: the queues, the last state seen, minted pins, and a log (`log.txt`).

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
