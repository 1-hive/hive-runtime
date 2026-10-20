# Reviewer runner

A reviewer that is a model behind a command: Astra (`openclaw agent exec --agent astra-reviewer …`), Codex (`codex exec`), or anything that reads a kickoff file and prints a review ending in `VERDICT: passed|failed|needs_information`. It runs as its own OS user with its own record key, so a review is signed by the reviewer, not by whoever asked for it.

    reviewer_runner.py --config /path/to/config.json

The config is the Iter adapter's ([`../iter/README.md`](../iter/README.md): `actor`, `hive_url`, `hive_id`, `key_file`, `registry`, `workspace`, `repos`), plus `review_cmd` (a list; `{kickoff}` is the kickoff file's path, `{text}` its contents), and optionally `poll_seconds` (30), `timeout_seconds` (5400) and `retry_seconds` (900).

It watches the record for results assigned to its actor that it hasn't judged. For each it writes a kickoff with the order, the result and each repository's `base..code` range (and the reviewer's own clone, if `repos` names one), runs the command in the workspace clone, takes the last `VERDICT:` line as the verdict, commits the output as the review report and records `review.recorded`, citing the result (A5). A run with no verdict is retried after `retry_seconds`; nothing is recorded for it.

Tested in `tests/iter_adapter_e2e.py` with a stand-in command.
