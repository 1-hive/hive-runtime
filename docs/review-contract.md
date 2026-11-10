# Review invocation contract (One Hive R5 ↔ R6)

The release plan's §4 asks for "how a worker session invokes an independent review, what the reviewer receives (the order, the pinned artifacts, nothing else), and what a recorded review verdict looks like as an event." This is that page, as hive-runtime implements it. The record's side (who may assign and record, and the close gate) is hive-record's SPEC and legality table (R2); this page doesn't change it.

## 1. Invocation: from the worker's own session

1. The worker posts its result: `task.result_posted` with the `result` pin, plus `code` and `base` pins for each repository the order names (worker contract, "Lifecycle" 5). The task moves to `in_review`. Posting is the request for a review.
2. In the same session, the worker waits for the verdict: `python3 {DIR}/review-wait.py <task>` (from 1-hive's launcher; each call returns within 9 minutes, so it fits a harness's command limit). Exit statuses are in the worker contract, "Lifecycle" 6.
3. A verdict that doesn't pass comes back to that session with the review report. The worker fixes the result with its context intact and posts again, up to `REVIEW_FIXES_IN_SESSION` (2) times per attempt. After that, or if the session has ended, the supervisor restarts the task with the review attached (class `failed_check`, which the router may move up a tier).

Long-lived agents (Iter, OpenClaw: `docs/long-lived-agents.md`) never end their session: their sidecar delivers the verdict into the agent's own runtime as a message ("Review of your result on …"), and the agent posts again through `hive_task` or `hive-task`.

## 2. Who reviews: independent by construction

- **The worker never chooses or contacts its reviewer.** The record allows `review.assigned` only to a coordinator (the dispatcher) or the operator, plus the chief of staff on 1-hive. It refuses a reviewer who is the result's author, and a `review.recorded` from anyone but the assigned reviewer.
- **Another harness or model.** The dispatcher picks the first reviewer in `HIVE_REVIEWERS` that isn't the author; with a review route table, hive-route picks by the review's tier, and its rule RT4 sends work by one model family to a reviewer of another. On 1-hive a reviewer runs in its own container, with its own key, under its own OS user (SPEC §6.6); a long-lived reviewer (the proto-hive's) holds its own key in its runner.
- **Blind.** The reviewer sees the record, not the worker's session: no transcript, chat or notes.

## 3. What the reviewer receives

Only what the record pins:
- the order, and on 1-hive its goal (its "Doesn't count, even if every check passes, when" line especially);
- the result pin (the report, or a whole workspace commit);
- for each repository the order names, the `base` and `code` pins: the review judges `git diff <base>..<code>` and runs the checks at `<code>` (`result-refs.sh`), not the branch;
- the worker contract's "Reviewers" section (re-run what matters, private text fails the review).

## 4. The verdict as an event

`review.recorded`, signed by the assigned reviewer:
- `data`: `{"verdict": "passed" | "failed" | "needs_information", "result_event": "<the result's event id>"}`;
- refs: `review`, the pin of the review report committed to workspace main (verdict, findings with evidence, what would change the verdict).

A verdict on any result but the current one is refused. `failed` and `needs_information` move the task back to `in_progress`, to its owner; `passed` keeps it `in_review` until the chief of staff closes it. The close is refused without a passed review of the current result (R2).

## 5. Invoked manually

- **The operator:** `op review <task> [reviewer]` assigns a review of the task's current result while it waits in review (again, for a second opinion after a verdict). The default reviewer is the first of `HIVE_REVIEWERS` that isn't the author. The dispatcher launches it; a long-lived reviewer's runner picks it up.
- **The chief of staff:** the same, through `review.assigned`.

A later verdict replaces the earlier one as the task's latest review: a failed second opinion sends the result back.

## 6. Limits

- A reviewer process is capped at 90 minutes. A worker waits up to `REVIEW_WAIT_MAX` (6000 s) per result, then ends its session; the review goes on without it.
- Reviews without the record (a review tool for a hive that has none, the plan's "standalone" use) aren't built. Every review here is a record event.
