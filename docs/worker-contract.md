# 1-hive worker contract

Every agent working a task in 1-hive (worker or reviewer) follows this. The project's own rules (e.g. `projects/<project>/WORKER.md` in the workspace) still apply for research and engineering practice. **Where they describe the lifecycle or approvals, this contract wins:** there is no human plan-approval gate, since the human approves goals, not steps.

## Identity

- You are exactly one actor, named in your kickoff. You sign every record event with **your own key only**, through the `hive` CLI:
  `HIVE_URL=http://127.0.0.1:8470 HIVE_ID=1-hive HIVE_KEY_FILE=<your key> hive emit …`
- Never read, copy or use another actor's key, including the operator's. Never write to the record any other way.
- Before each event, read the task first (`hive task <id>`) so `expected_revision` is current. The CLI sends it for you.

## Where you work

- A task-scoped directory, named in your kickoff, holding your own clones of the workspace and the code repositories.
- Code changes go on the branch `hive/<task-id>`, pushed to the local remote. Never push to `main`: merging is decided after review.
- Commit and push before you refer to anything. The record holds pins, not content: `hive-pin mint <repo> <path> --commit <sha>` for a file or directory, `hive-pin mint <repo> --commit <sha>` for a whole commit, using the registry in your task directory.
- The order's `ext.repos` (`hive task <id>`) names the code repositories you may change. `base.json` in your task directory holds the commit each one's branch started from.

## Running mode

Workers run non-interactively: **your session ends the moment you stop to wait.** Never end a turn to wait for a background job, a monitor or a notification. Anything you start in the background is killed when you stop. Run long work in the foreground, in pieces that fit a command's time limit (up to 10 minutes each), and keep going until you've posted a result or a block. Before you finish, stop anything you started (containers, games).

## Lifecycle

1. **Accept:** `hive emit task.accepted --task <id>`, once you have read the order and this contract.
2. **Plan:** write a short plan (question, approach, deliverables, how you'll check them, risks) as a report in the workspace, then `task.reported` with `kind: progress` (its data is exactly `{"kind": ...}` and its only ref is `report`: `--data '{"kind":"progress"}' --ref report=<pin>`; `ext` holds only a measured `cost`, never your own fields; everything else goes in the report). Then **continue**; don't wait for approval.
3. **Check in:** at least once per `checkin_every` interval on your lease, commit your progress and emit `task.reported` with `kind: checkpoint`. The checkpoint report says what's done, what's next, and anything a replacement would need to continue. A supervisor may restart you from your last checkpoint.
4. **Blocked:** only for a decision outside the order's scope, missing access, or an external blocker. Commit a question file (decision, options, recommendation, evidence, safe default), then `task.blocked` with `needs` and the question pin. Stop until `task.answered`, then read the answer and `task.unblocked`. The chief of staff answers what it can and escalates the rest to the human.
5. **Result:** commit the result report (question, conclusion and confidence, evidence and reproduction, limitations, recommended next task, changed commits and branches), then `task.result_posted` with the result pin and, for each repository in the order's `repos` (if it names none, add no code refs), its code and base:
   `--ref result=<report pin> --ref code=<pin of your pushed branch tip> --ref base=<pin of base.json's commit>`
   For each repository `R` in `ext.repos` (your clone is `{DIR}/R`), mint them with `hive-pin mint R --commit "$(git -C R rev-parse HEAD)"` and `hive-pin mint R --commit "$(jq -r --arg r R '.[$r]' base.json)"`, and add one `code` and one `base` ref per repository. Push only your `hive/<task>` branch, never a `main`. The gateway refuses a result whose code is missing, names a repository the order doesn't, or doesn't descend from its base. Don't post a result because time ran out; post a checkpoint and say so.
6. **After a failed review** or `needs_information`, you are restarted with the review attached. Address it and post a new result.

## Routing checkpoints

Your model was chosen for this attempt from what was known about the task when it started. If that changes, end your attempt at a checkpoint, and the next attempt is routed again:

- **Down:** you've done the part that needed a strong model (e.g. the plan or the diagnosis), and the rest is explicit work you've written down. Set `specification` to `explicit`, and `scope` (`single`, `few`, `many`) if the rest touches less than the whole task.
- **Up:** the task needs more than you can give it at this tier. Name the `tier` it needs (`standard` or `strong`) and say why.

To checkpoint: commit and pin a checkpoint report that a fresh agent, possibly on another model, can continue from (what's done, the plan for the rest and how it's checked, open questions), emit `task.reported` with `kind: checkpoint`, then write `{DIR}/route-checkpoint.json` and end your session:

    {"direction": "down", "reason": "<one line>", "facts": {"specification": "explicit", "scope": "few"}}
    {"direction": "up", "reason": "<one line>", "tier": "strong"}

The supervisor checks the request and restarts you; it isn't a failed attempt. Only `specification` and `scope` can change: what's at stake (`verification`, `consequence`, `leverage`) stays as the chief of staff set it. A task gets at most 3 checkpoints. Don't checkpoint just because you're tired of the task, and never instead of posting a result you can post.

## Reviewers

- You review the task named in your kickoff: its current result, against its order. Check claims by re-running what matters.
- **The code you review is the result's pins, not the branch.** `./result-refs.sh <task-id>` prints `<repo> <base> <code>` for each code repository; review `git diff <base>..<code>` there and run the checks at `<code>` (`git checkout <code>`). If it prints nothing, the order named no repositories: review the commits the report names. Everything in that range is part of the change, including edits the report doesn't mention. If you materialize a pin and hivepin lists `omitted` entries (symlinks or submodules it didn't create), name them in the review, and fail it if the change touches any of them. Write a review report (verdict, findings with evidence, what would change the verdict), commit it, then `review.recorded` with `verdict` ∈ {passed, failed, needs_information}, `result_event` (the result's event id) and the review pin.
- You are independent: don't coordinate with the author, and don't fix the work yourself.

## Stop-lines

- Stay inside the order's scope. Anything outside it is a new task for the chief of staff, not something to do yourself. Code changes go only in the repositories the order names.
- No credentials, paid resources or external publication unless the order says so.
- No destructive actions on shared state: other branches, other tasks' directories, the record's database, other agents' sessions.
