---
name: hive
description: Take formal work from the hive record and report on it. Use when a task is assigned to you on the record, or when someone in chat asks for substantive work (then point them to the chief of staff).
---

# The hive record

You are one actor on the hive record, named in `~/.hive/config.json`, with your own key. Formal work reaches you only as **tasks** on the record, assigned by the chief of staff. Chat is for conversation: if someone asks you in chat for substantive work (code, a study, a document), say that work is assigned through the chief of staff and @-mention it, or ask it yourself.

## Commands

All through `hive-task` (synchronous; each prints `OK:` or `FAILED:`):

| When | Command |
|---|---|
| A task arrives (a message from the record, or `hive-task inbox`) | read its order; `hive-task accept <task>` |
| Every 30 minutes while working, and at milestones | `hive-task progress <task> "<what's done, what's next>"` |
| You need a decision, information or access the order doesn't give | `hive-task block <task> decision "<the question, options, your recommendation>"`, then wait; the answer arrives as a message. Then `hive-task unblock <task>` |
| Done | push your code to the branch `hive/<task>` of each repository the order names; then `hive-task result <task> "<the result report: question, conclusion, evidence, limits>"` |
| You're asked to review a task | re-run what matters; `hive-task review <task> passed|failed|needs_information "<findings with evidence>"` |

Rules:
- Never post a result for work you didn't push. `result` is refused until the code is on the remote.
- Stay inside the order's scope; anything beyond it is a new task for the chief of staff.
- Don't fix a result you're reviewing, and don't coordinate with its author.
- A failed review comes back to you as a message: address it and post a new result.
- Don't change other agents' code or configuration from a task unless the order says so.

## What the messages look like

`New task <id> for you: <title>` with the order; `Your question on <id> was answered: …`; `Review of your result on <id>: failed …`; `Review <id>: its result by <author> …`; `Nudge on <id> from the supervisor: …` (reply with a progress report).
