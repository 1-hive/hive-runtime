#!/usr/bin/env bash
# Headless Claude Code (`claude -p ...`) on the first Claude account that isn't at its usage limit.
#
#   CLAUDE_TOKEN_FILES="/keys/claude-oauth-token /keys/claude-oauth-token.2" \
#   CLAUDE_FAILOVER_STATE=/some/dir  claude-failover.sh <claude args...>   < prompt
#
# Each token file holds one account's `claude setup-token` token, in order of preference (missing or empty files
# are skipped). The call runs on the first account, with --output-format json so that an API error is told apart
# from a reply that merely mentions a limit. When it ends in an API error that is the account's (a usage or spend
# limit, HTTP 429/402, or the token refused, 401), that account is marked limited in CLAUDE_FAILOVER_STATE for
# CLAUDE_FAILOVER_COOLDOWN seconds (default 1800) and the same call runs again on the next account. A marked
# account is tried last until its mark expires, so a run of calls doesn't hit the limit each time.
# Prints the answer's text (the JSON "result") and exits with claude's status. When every account is limited, that
# is the last limit message (the supervisor's capacity check reads it as one).
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
# The answer's text from claude's JSON result on stdout (raw output if it isn't one), and exit 3 when the call
# ended in an API error that belongs to the account.
PARSE='import json,re,sys
raw=open(sys.argv[1],encoding="utf-8",errors="replace").read()
try:
    r=json.loads(raw.strip().splitlines()[-1])
except (ValueError,IndexError):
    sys.stdout.write(raw); sys.exit(0)
text=r.get("result") or ""
sys.stdout.write(text + ("\n" if text and not text.endswith("\n") else ""))
acct=r.get("api_error_status") in (401,402,429) or re.search(r"hit your limit|usage limit|spend limit|limit reached|credit balance|out of credits|billing_error|rate_limit",text,re.I)
sys.exit(3 if r.get("is_error") and acct else 0)'
order=()
for f in "${TOKENS[@]}"; do limited "$f" || order+=("$f"); done
for f in "${TOKENS[@]}"; do limited "$f" && order+=("$f"); done   # marked ones last, in case all are

if [ "${1:-}" = --pick ]; then [ ${#order[@]} -gt 0 ] && echo "${order[0]}"; exit 0; fi
# No token files: run as configured (claude.env, an existing login).
[ ${#order[@]} -gt 0 ] || exec "$CLAUDE_BIN" "$@"

tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
cat > "$tmp/in"
i=0
for f in "${order[@]}"; do
  i=$((i + 1))
  rc=0
  CLAUDE_CODE_OAUTH_TOKEN=$(tr -d '[:space:]' < "$f") "$CLAUDE_BIN" "$@" --output-format json < "$tmp/in" > "$tmp/raw" 2> "$tmp/err" || rc=$?
  lim=0; python3 -c "$PARSE" "$tmp/raw" > "$tmp/out" || lim=$?
  if [ "$lim" = 3 ]; then
    echo $((now + COOLDOWN)) > "$(mark "$f")" 2>/dev/null || true
    if [ "$i" -lt ${#order[@]} ]; then
      echo "claude-failover: $(basename "$f") is at its usage limit or refused; using $(basename "${order[$i]}")" >&2
      continue
    fi
  elif [ "$rc" -eq 0 ]; then
    rm -f "$(mark "$f")" 2>/dev/null || true
  fi
  cat "$tmp/out"; cat "$tmp/err" >&2
  exit "$rc"
done
