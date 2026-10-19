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

Iter gives a channel or tool call 5 seconds. So the channel starts a small daemon on first use, and the tool only queues requests for it. The daemon reads the record, commits and pushes reports to the workspace, mints pins, and emits signed events with `hive` and `hive-pin`.

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

`tests/iter_adapter_e2e.py` runs a worker and a reviewer on a scratch record, the way `iter.py` calls channels and tools (a new process per call, from the agent's folder). The task goes through:
1. assigned, accepted;
2. progress, through the tool and through `send`;
3. blocked, answered, unblocked;
4. a result refused until pushed, then a result with code and base pins;
5. a failed review, a new result, a passing review;
6. closed.

```
uv run --project ~/repos/hive-record python tests/iter_adapter_e2e.py
```
