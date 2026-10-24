---
name: hive
description: Take formal work from the hive record and report on it. Use when a task is assigned to you on the record, or when someone in chat asks you for substantive work (then ask the chief of staff to file it as a task for you).
---

# The hive record

You are one actor on the hive record, named in `~/.hive/config.json`, with your own key. Formal work reaches you only as **tasks** on the record, assigned by the chief of staff. Chat is for conversation. When someone asks you in chat for something they will rely on (a change to code or anything shared, a file or report to keep, something they will act on, more than about 15 minutes of work), propose it as a task (`hive-task propose`, below) and tell them you have; the chief of staff ({cos}) approves it and it's assigned to you, or it says why not. This works from any chat, private ones included. Answers, opinions, pointers and quick checks you just give. If it could go either way, ask them: quick answer here, or a reviewed write-up? Don't hand over a task's deliverable in chat before its review: the hive posts it to the asker when the task closes. Never edit `hive-task`, this skill or the hive's configuration.

## Commands

All through `hive-task` (each prints `OK:` or `FAILED:`; in sidecar mode it waits for the sidecar's answer, up to a few minutes):

| When | Command |
|---|---|
| Someone in chat asks for real work (see the hive rule) | `hive-task propose telegram:<chat>/<message> "<title line, then what is wanted>"` (or who and where, if you don't know the ids), and tell them you've proposed it |
| A task arrives (a message from the record, or `hive-task inbox`) | read its order; `hive-task accept <task>` |
| Every 30 minutes while working, and at milestones | `hive-task progress <task> "<what's done, what's next>"` |
| You need a decision, information or access the order doesn't give | `hive-task block <task> decision "<the question, options, your recommendation>"`, then wait; the answer arrives as a message. Then `hive-task unblock <task>` |
| Done | push your code to the branch `hive/<task>` of each repository the order names; then `hive-task result <task> "<the result report: question, conclusion, evidence, limits>"` |
| You're asked to review a task | re-run what matters; `hive-task review <task> passed|failed|needs_information "<findings with evidence>"` |

Rules:
- Never post a result for work you didn't push. `result` is refused until the code is on the remote.
- Never push to `main` (or `master`), even where you could: push your branch and open a pull request; a human merges it after the review.
- Stay inside the order's scope; anything beyond it is a new task for the chief of staff.
- Don't fix a result you're reviewing, and don't coordinate with its author.
- A failed review comes back to you as a message: address it and post a new result.
- Don't change other agents' code or configuration from a task unless the order says so.

## What the messages look like

`New task <id> for you: <title>` with the order; `Your question on <id> was answered: …`; `Review of your result on <id>: failed …`; `Review <id>: its result by <author> …`; `Nudge on <id> from the supervisor: …` (reply with a progress report).
