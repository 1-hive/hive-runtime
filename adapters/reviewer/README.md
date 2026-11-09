# Reviewer runner

A reviewer that is a model behind a command: Astra (`openclaw agent exec --agent astra-reviewer …`), Codex (`codex exec`), or anything that reads a kickoff file and prints a review ending in `VERDICT: passed|failed|needs_information`. It runs as its own OS user with its own record key, so a review is signed by the reviewer, not by whoever asked for it.

    reviewer_runner.py --config /path/to/config.json

The config is the Iter adapter's ([`../iter/README.md`](../iter/README.md): `actor`, `hive_url`, `hive_id`, `key_file`, `registry`, `workspace`, `repos`), plus `review_cmd` (a list; `{kickoff}` is the kickoff file's path, `{text}` its contents), and optionally `poll_seconds` (30), `timeout_seconds` (5400) and `retry_seconds` (900).

It watches the record for results assigned to its actor that it hasn't judged. For each it writes a kickoff with the order, the result and each repository's `base..code` range (and the reviewer's own clone, if `repos` names one), runs the command in the workspace clone, takes the last `VERDICT:` line as the verdict, commits the output as the review report and records `review.recorded`, citing the result (A5). A run with no verdict is retried after `retry_seconds`; nothing is recorded for it. Neither is a verdict with almost nothing before it (under 400 characters): a headless session that a background task woke after its review can end on a two-line message, and `claude -p` keeps only that, so the runner retries rather than record a pass without findings (test: `tests/reviewer_thin_review_test.py`). Run Claude Code reviewers with `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` so their last message is the review. A task it has reviewed already runs again when the record assigns its review to the same reviewer after that review (`review.assigned` naming it, at a later position): how whoever assigns reviews sends one back (test: `tests/reviewer_assigned_again_test.py`).

When the command's output says the reviewer's model account hit a usage, spend or credit limit, the runner's beacon reports "its model account hit a usage limit: <the line>" instead of a missing verdict, so the Floor shows the reviewer can't run and the operators get one alert (test: `tests/reviewer_limit_test.py`).

**Another reviewer on the same machine:** `add-reviewer.sh` copies a running reviewer's setup (harness login, git routes, settings, systemd unit) for a new actor with its own OS user and record key, usually on another model. `add-reviewer.sh setup …` prints the public keys for the operators to register; after registration, `add-reviewer.sh start …` clones the workspace and starts the unit. Run it without arguments for its usage.

Tested in `tests/iter_adapter_e2e.py` with a stand-in command.
