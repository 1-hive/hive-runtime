# Putting long-lived agents on the hive

For a hive whose agents already run all the time (chat bots with their own memory, often self-modifying) rather than being launched per task. The agents keep running and keep their memory; the record takes over formal work: who is assigned what, results pinned, every result reviewed before close. The first hive adopted this way is the proto-hive (2026-10-22: five bots, two machines, switched without restarting any bot).

This complements One Hive's interfaces: the runtime launches and delivers, and doesn't prescribe the agent's cognition. A long-lived agent and a bounded worker share the same record contract; only how work reaches them differs.

## The shape

- **The record** (hive-record) and its gateway, one per hive. The `proto-hive` profile suits chat bots: no goals and no human approvals, the order carries its goal, criteria and a "useless if" line; leases, supervision and cost stay.
- **A chief of staff** (`cos.sh`, role doc `docs/chief-of-staff-proto-hive.md`), reached through a chat work group by hive-bridge. It is the only way work is assigned.
- **A sidecar per agent** (`adapters/iter/sidecar.py`): holds the agent's record key, does the record and git work, and talks to the agent only through a queue folder. The agent never holds its key.
- **A reviewer** with its own key and a different model from the workers (`adapters/reviewer/reviewer_runner.py`).
- **The dispatcher and supervisor** in long-lived mode (`HIVE_LONG_LIVED_ACTORS`, `HIVE_REVIEWERS`): reviews assigned automatically; nudges delivered as messages, then escalation to the chief of staff; never a restart.

## How work reaches each kind of agent

Work reaches an agent through its own runtime, never through chat: telling a bot it has a task is a control signal, not conversation ("runtime is not speech").

| Agent | How it hears | Set up with |
|---|---|---|
| Iter | Iter checks its channels every cycle; the hive channel reads the sidecar's queue | `install-agent.py iter --folder F` (channel, tool, prompt rule) |
| OpenClaw | the sidecar starts a turn of the agent with each message through the gateway's inbound webhook (`/hooks/agent`, a token of its own, one agent only, nothing posted to chat) | `install-agent.py openclaw … --hook-token-file F`, a gateway restart, and the sidecar's `"deliver": [<python>, "adapters/openclaw/hook-deliver.py", "--url", "http://127.0.0.1:<port>/hooks/agent", "--token-file", F, "--agent", "main", "{text}"]` |
| Anything with a shell but no way to start a turn | it runs `hive-task inbox` itself (a heartbeat or a scheduled job) | `hive-task` in sidecar mode; a fallback: it costs a turn each time, and a disabled heartbeat leaves work unseen |

If the gateway listens only on loopback inside a container and the sidecar runs on the host, the sidecar can't reach it directly. Either open the gateway to the container network (tokens still apply) or give the sidecar a narrow root helper that enters only that gateway's network namespace to make the one call (the proto-hive does the latter, through one sudo rule per bot).

Each install is idempotent, backs up every file it changes as `.pre-hive`, and is reversed with `--undo`. Agents in containers: `--map HOST=AGENT` translates paths written for the agent.

## Keys when agents share an OS user

The record's SPEC §6.6 asks that no two agents share a process, container, filesystem, OS user or key. Where the existing setup can't give that yet:

- **Keep keys apart anyway.** Run each sidecar under its own OS user, with its key readable only there. Share only the queue folder with the agent (ACLs work well: they grant exactly that user exactly that folder, without loosening anything else).
- **The sidecar only reads the agent's clones.** It never fetches or writes into them (it checks that a result was pushed with `git ls-remote`), so it can run as another user without leaving files the agent can't use.
- **Keep the reviewer out of the workers' reach.** Its model, prompt and tools must not live in a configuration the workers can edit, or a worker can shape its own review.
- **Declare the deviation.** Each actor's declaration says how its sandbox is shared, and the adoption note says plainly that the deployment doesn't conform: on such a machine, an event signed by an agent's key proves only that some process there asked that agent's sidecar.

## Self-modifying agents

Run `adapters/iter/conformance.py --folder F` on an Iter agent after installing the adapter and whenever its Iter changes (an upgrade, a merge, its own edits): it checks the adapter still works under that agent's own loader and limits, without touching its queue.

Agents may change their own code; the sidecar re-declares the agent on the record when its code changes (`declare_seconds`, default 600): the folder's commit, a hash of uncommitted code, the hive adapter it holds, and any `declare_files` (for instance a memory system's switches). Changing another agent's code is a task through the chief of staff. The hive's own machinery (the shared repositories, the deployment, the adapters, the reviewer, the chief of staff) stays off limits to every agent.

## Code repositories

- Register every repository the agents may work in (the proto-hive registers all of one account's repositories; ids must be lower case).
- **After any change to the registry, an operator records it** (`hive emit hive.registry_changed` with the new digest, OPERATOR_GUIDE "Changing the registry"); until then the gateway refuses every pin.
- The sidecar finds the agent's clone of a repository by its remote (`repo_search`), so its config needn't list them.
- If agents can push to `main` (they act as the account that owns the repositories), make "branch, pull request, a human merges" their rule and run `main-watch.py`, which reports any commit reaching a default branch without a passed review.

## Review before publishing: staged repositories

A result is pinned, and the pin must be published where the reviewer and anyone later can fetch it. If that place is the repository's public home, unreviewed work is public the moment it's posted, and a secret in it has leaked even if the review fails. So a hive can **stage** its repositories:

- **The registry lists a hive copy first:** `"fetch_urls": ["file:///var/lib/hive/repos/<repo>.git", "https://github.com/<owner>/<repo>.git"]`. A pin counts as published if any listed address has the commit; the hive copy holds work in review, and the home everything else.
- **The sidecar puts each result in the hive copy** as `hive/<task>` (`hive/<task>-r2`… when a later result doesn't fast-forward), so agents need no push access to the home at all. Machines other than the record's reach the copy over `hive-git-shell` (one git setting per user: `url."ssh://hive@<host>/var/lib/hive/repos/".insteadOf "file:///var/lib/hive/repos/"`), whose pre-receive hook lets agents add only `hive/*` and rewrite or delete nothing.
- **The reviewer runner fetches `hive/*` from the copy** before each review.
- **`publisher.py`**, from a timer: keeps each copy mirrored from its home (except `hive/*`); for each passed review not yet published, scans what the result adds for secrets and the hive's `--deny` patterns (e.g. real chat ids) and, if clean, pushes that exact commit to the home's `hive/<task>` with the repository's push key, then posts the pull-request link. Push keys live only with the publisher (`--keys DIR`: `DIR/<repo>`, a write deploy key per repository, or `DIR/default`). A human merges.
- **A repository that isn't staged** (one URL, or the home first) works as before: the agent pushes a branch before posting its result.

## Chat stays open

The agents keep talking to people and to each other. Substantive work asked in chat is filed by the agent itself, through the chief of staff (the rule `install-agent.py` adds to the agent's prompt, `adapters/hive-rule.txt`); small things are just answered. The record holds formal work; the bridge's dialogue log holds the conversation.

## Seeing what the agents do: the Floor (optional)

hive-bridge's Floor is a read-only page over the record: each agent's card (what it's doing now, what it owes, when it last spoke), the work by stage, and what's waiting on people (escalations, questions, reviewed code not yet merged, the chief of staff's requests). It is optional: an operator's view, never part of the record's rules. What the record can't know (is the agent busy, chatting, offline) comes from **beacons**, which each sidecar and the reviewer runner post every 30 s, signed with the agent's key and carrying ids and times only, never message text (`adapters/iter/README.md`, "Beacons"). Give the Floor's user read access to the repository clones it checks for merges.

## Pitfalls met on the way

- Python installed by `uv` under root's home: other users can't run it. Set `UV_PYTHON_INSTALL_DIR` under the shared install folder.
- An agent on another machine than the record pins its reports in the workspace, and the pin check looks for the commit at the registry's address for it, which may be a path on the record's host. Point that address at the agent's real route with git (`url.<ssh address>.insteadOf <file:// address>`, per user), as the chief of staff does; otherwise every report fails with `REMOTE_UNAVAILABLE`.
- A config file rewritten as root (a token, a group list) stops a service that runs as another user.
- Long lines pasted into a terminal can break in two; prefer scripts and short lines.
- A reviewer that can't run leads to escalations; the supervisor waits a full window after each one (v0.4.14), so they don't repeat every pass.
