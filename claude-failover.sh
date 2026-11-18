#!/usr/bin/env bash
# Headless Claude Code (`claude -p ...`) on the first Claude account that isn't at its usage limit.
#
#   CLAUDE_TOKEN_FILES="/keys/claude-oauth-token /keys/claude-oauth-token.2" \
#   CLAUDE_FAILOVER_STATE=/some/dir  claude-failover.sh <claude args...>   < prompt
#
# Each token file holds one account's `claude setup-token` token, in order of preference (missing or empty files
# are skipped). The call runs on the first account, with --output-format stream-json so that an API error is told
# apart from a reply that merely mentions a limit, and the model that answered is known. When it ends in an API error that is the account's (a usage or spend
# limit, HTTP 429/402, or the token refused, 401), that account is marked limited in CLAUDE_FAILOVER_STATE for
# CLAUDE_FAILOVER_COOLDOWN seconds (default 1800) and the same call runs again on the next account. A marked
# account is tried last until its mark expires, so a run of calls doesn't hit the limit each time.
# Prints the answer's text (the JSON "result") and exits with claude's status; with CLAUDE_FAILOVER_MODEL_FILE set,
# writes the model that wrote it there (claude-result.py: nothing when the output doesn't say). When every account
# is limited, that is the last limit message (the supervisor's capacity check reads it as one). Without token files
# the call runs once, as configured.
# `claude-failover.sh --pick` prints the token file to use now (for an interactive session started elsewhere).
set -uo pipefail
STATE=${CLAUDE_FAILOVER_STATE:-${CLAUDE_CONFIG_DIR:-$HOME/.claude}}
COOLDOWN=${CLAUDE_FAILOVER_COOLDOWN:-1800}
CLAUDE_BIN=${CLAUDE_BIN:-claude}
read -ra FILES <<< "${CLAUDE_TOKEN_FILES:-}"
TOKENS=()
for f in "${FILES[@]}"; do [ -s "$f" ] && TOKENS+=("$f"); done
mkdir -p "$STATE" 2>/dev/null || true
now=$(date +%s)

mark() { echo "$STATE/claude-limited.$(basename "$1")"; }
limited() { local m; m=$(mark "$1"); [ -s "$m" ] && [ "$(cat "$m" 2>/dev/null || echo 0)" -gt "$now" ] 2>/dev/null; }
# The answer's text from claude's output (claude-result.py), and exit 3 when the call ended in an API error that
# belongs to the account. CLAUDE_FAILOVER_MODEL_FILE, when set, receives the model that wrote the answer.
RESULT="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/claude-result.py"
MODEL_ARGS=(); [ -n "${CLAUDE_FAILOVER_MODEL_FILE:-}" ] && MODEL_ARGS=(--model-file "$CLAUDE_FAILOVER_MODEL_FILE")
order=()
for f in "${TOKENS[@]}"; do limited "$f" || order+=("$f"); done
for f in "${TOKENS[@]}"; do limited "$f" && order+=("$f"); done   # marked ones last, in case all are

if [ "${1:-}" = --pick ]; then [ ${#order[@]} -gt 0 ] && echo "${order[0]}"; exit 0; fi
# No token files: one call as configured (claude.env, an existing login).
[ ${#order[@]} -gt 0 ] || order=("")

tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
cat > "$tmp/in"
i=0
for f in "${order[@]}"; do
  i=$((i + 1))
  rc=0
  if [ -n "$f" ]; then
    CLAUDE_CODE_OAUTH_TOKEN=$(tr -d '[:space:]' < "$f") "$CLAUDE_BIN" "$@" --output-format stream-json --verbose \
      < "$tmp/in" > "$tmp/raw" 2> "$tmp/err" || rc=$?
  else
    "$CLAUDE_BIN" "$@" --output-format stream-json --verbose < "$tmp/in" > "$tmp/raw" 2> "$tmp/err" || rc=$?
  fi
  lim=0; python3 "$RESULT" "$tmp/raw" "${MODEL_ARGS[@]}" > "$tmp/out" || lim=$?
  if [ -n "$f" ] && [ "$lim" = 3 ]; then
    echo $((now + COOLDOWN)) > "$(mark "$f")" 2>/dev/null || true
    if [ "$i" -lt ${#order[@]} ]; then
      echo "claude-failover: $(basename "$f") is at its usage limit or refused; using $(basename "${order[$i]}")" >&2
      continue
    fi
  elif [ -n "$f" ] && [ "$rc" -eq 0 ]; then
    rm -f "$(mark "$f")" 2>/dev/null || true
  fi
  cat "$tmp/out"; cat "$tmp/err" >&2
  exit "$rc"
done
