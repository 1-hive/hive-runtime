# Skills in the runtime (One Hive R4)

A task's agents get their methods as **skills**: [Agent Skills](https://agentskills.io) `SKILL.md` directories,
which Claude Code, Codex and OpenClaw all read. The hive approves each one, by content hash and for a limited time,
in its skills registry. [hive-skills](https://github.com/1-hive/hive-skills) is the registry and loader; its
[SPEC](https://github.com/1-hive/hive-skills/blob/main/SPEC.md) is the R4 ↔ R6 contract this runtime follows.

## What the runtime ships

`skills/hive-worker/`: the worker contract (`SKILL.md`, formerly `docs/worker-contract.md`) with its scripts
`scripts/review-wait.py` (R5: wait for the review in the session) and `scripts/result-refs.sh` (a reviewer's
`base`/`code` pins). It is for workers and reviewers.

## What the launcher does

On every attempt, once the router has chosen the harness, `launch-task.sh` runs `load-skills.sh <folder> <role>
<harness>`:

- `hive-skill load` installs the registered skills for that role and harness into `<folder>/.agents/skills/`,
  verified against their pins; `<folder>/.claude/skills` links to it. A restart gets the version registered now, and
  an installed copy the agent edited is restored.
- `WORKER-CONTRACT.md`, `review-wait.py` and `result-refs.sh` in the folder are links into the `hive-worker`
  skill, so the kickoffs, the contract and the supervisor's restart context name the same paths as before.
- The load report (the skills, their content hashes, and `set_digest`, one digest for the set) is kept with the
  attempt: `$HIVE_WORK_DIR/.route/<folder>/<role>.<n>.skills.json`, next to its route manifest.
- A refusal (an expired skill, two that conflict, a pin that no longer verifies, an unregistered skill in the way)
  stops the launch: a task never runs without the skills it was meant to have.

The registry is `HIVE_SKILLS_REGISTRY`, else `$HIVE_DEPLOY_DIR/skills.json`. A hive without one gets the runtime's
own `skills/hive-worker` copied in, unregistered, as before R4.

## Keeping the registry current

The registry pins one commit of each skill, so a new runtime release doesn't change what agents load until the
operator registers it. When a release changes `skills/hive-worker`:

```
hive-skill --registry deploy/skills.json register hive-runtime skills/hive-worker --commit <tag> --replace
```

and commit `skills.json` with the pin bump. 1-hive's `up.sh` warns when the deployed runtime ships a skill that
differs from the registered one, and lists skills that expire within 14 days. `op skills` shows the registry and
any drift in the open task folders (`hive-skill check`).
