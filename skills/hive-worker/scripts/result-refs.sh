#!/usr/bin/env bash
# The code a task's current result claims to have changed, from the record.
#
#   result-refs.sh <task-id>        (needs HIVE_URL, HIVE_ID, HIVE_KEY_FILE: source hive.env)
#
# Prints one line per code repository: "<repo> <base-sha> <code-sha>". Review
# `git diff <base>..<code>` in that repository: exactly the task's change. A result
# with no code refs (a research or review task) prints nothing.
set -euo pipefail
TASK=$1
hive events --task "$TASK" --type task.result_posted | tail -n 1 | jq -r '
  [.refs[] | select(.rel == "code" or .rel == "base")] as $r
  | $r[] | select(.rel == "code") | .pin as $c
  | ($r[] | select(.rel == "base" and .pin.repository == $c.repository) | .pin) as $b
  | "\($c.repository) \($b.commit_oid | sub("^sha[0-9]+:"; "")) \($c.commit_oid | sub("^sha[0-9]+:"; ""))"'
