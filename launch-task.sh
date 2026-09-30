#!/usr/bin/env bash
# Prepare a task folder for one actor and launch its session.
# Minimal stand-in for the Phase E launcher: same steps, no containers yet.
#
#   launch-task.sh worker   <task-id> <actor-id> <kickoff-file> [base-branch]
#   launch-task.sh reviewer <task-id> <actor-id> <kickoff-file>
#
# worker:   in ~/work/1hive/<task-id>, mtg-player on a new branch hive/<task-id> from
#           base-branch (default main). Re-running for an existing folder relaunches in it
#           (a restart).
# reviewer: in ~/work/1hive/<task-id>-review, mtg-player on hive/<task-id>.
#
# The kickoff file may use {DIR} for the task folder; it is copied to KICKOFF.md.
#
# The router (hive-route) picks the harness, model and effort for every attempt; the
# route block below. Optional environment for the router (ROUTING.md §4):
#   ROUTE_FACTS       JSON object of task facts, e.g. '{"specification":"explicit",
#                     "verification":"independent","scope":"few","consequence":"reversible"}'
#   ROUTE_HINT        JSON {"tier": ..., "reason": ...}: raises the tier, never lowers it
#   ROUTE_LAST_CLASS  failure class of this role's previous attempt on the task (§5):
#                     outage, capacity, truncated, missing_info, failed_check, stalled,
#                     indeterminate, interrupted
#   ROUTE_REASON      reassign, to move off the previous route
# Claude Code runs in print mode with auto permissions; Codex runs `exec`.
set -euo pipefail
ROLE=$1 TASK=$2 ACTOR=$3 KICKOFF=$4 BASE=${5:-main}
ROOT=$HOME/work/1hive
DIR=$ROOT/$TASK; [ "$ROLE" = reviewer ] && DIR=$ROOT/$TASK-review
SEED_BUILD=${SEED_BUILD:-$ROOT/hbp-1-feasibility/mtg-player/adapters/xmage-external-seat/.build}
KEY=$HOME/.config/hive/agents/$ACTOR.key
[ -f "$KEY" ] || { echo "no key for $ACTOR" >&2; exit 2; }

if [ ! -d "$DIR" ]; then
  mkdir -p "$DIR"
  git clone -q /home/omegahive/repos/hive-workspace.git "$DIR/workspace"
  if [ "$ROLE" = worker ]; then
    git clone -q -b "$BASE" /home/omegahive/repos/mtg-player.git "$DIR/mtg-player"
    git -C "$DIR/mtg-player" checkout -q -b "hive/$TASK"
  else
    git clone -q -b "hive/$TASK" /home/omegahive/repos/mtg-player.git "$DIR/mtg-player"
  fi
  if [ -d "$SEED_BUILD" ]; then
    cp -a "$SEED_BUILD" "$DIR/mtg-player/adapters/xmage-external-seat/.build"
  fi
  python3 - "$DIR" <<'EOF'
import json, sys
d = sys.argv[1]
reg = json.load(open("/home/omegahive/repos/1-hive/deploy/registry.json"))
reg["repositories"]["workspace"]["local_path"] = f"{d}/workspace"
reg["repositories"]["mtg-player"]["local_path"] = f"{d}/mtg-player"
json.dump(reg, open(f"{d}/registry.json", "w"), indent=2)
EOF
fi
cp /home/omegahive/repos/1-hive/docs/worker-contract.md "$DIR/WORKER-CONTRACT.md"
sed "s#{DIR}#$DIR#g" "$KICKOFF" > "$DIR/KICKOFF.md"
git -C "$DIR/workspace" pull -q --ff-only || true
cd "$DIR"

# ---- route: ask the router which harness and model run this attempt ------------------
# Table deploy/route-table.yaml; pool usage and qualifications from deploy/route-sources.yaml.
# Every decision goes to $ROOT/route-log.jsonl (created in fixed mode); each attempt gets a
# manifest (<role>.<n>.attempt.json) so the router can check which model actually ran.
TABLE=/home/omegahive/repos/1-hive/deploy/route-table.yaml
SOURCES=/home/omegahive/repos/1-hive/deploy/route-sources.yaml
RLOG=$ROOT/route-log.jsonl
hr() { uv run -q --frozen --project /home/omegahive/repos/hive-route hive-route "$@"; }
[ -f "$RLOG" ] || hr mode fixed "$TABLE" --log "$RLOG" >/dev/null
# Earlier attempts' reported models against their routes: logs drift, demotes the route.
hr observe "$RLOG" "$ROOT/*/*.attempt.json" --sources "$SOURCES" >&2 || true

# Attempt numbers count this role's earlier outputs, whatever harness wrote them.
if [ "$ROLE" = worker ]; then KIND=work; PREFIX=worker; OLD="worker-*.jsonl worker-*.log"
else KIND=review; PREFIX=review; OLD="codex-*.log review-*.jsonl review-*.log"; fi
n=$( (ls $OLD 2>/dev/null || true) | wc -l)
ATTEMPT=$TASK.$PREFIX.$n
REASON=${ROUTE_REASON:-new}; [ "$n" -gt 0 ] && [ -z "${ROUTE_REASON:-}" ] && REASON=restart

# This role's earlier attempts on the task, with their failure classes where known.
HIST=$DIR/route-history.jsonl; touch "$HIST"
if [ -n "${ROUTE_LAST_CLASS:-}" ] && [ -s "$HIST" ]; then
  { head -n -1 "$HIST"; tail -n 1 "$HIST" | jq -c --arg c "$ROUTE_LAST_CLASS" '. + {class: $c}'; } > "$HIST.tmp"
  mv "$HIST.tmp" "$HIST"
fi
HISTORY=$(jq -s -c '[.[] | select(.class) | {attempt, route_id, tier, pool, class}]' "$HIST")

AUTHOR=null
if [ "$ROLE" = reviewer ]; then
  # The author is the task's latest worker attempt; before routing, the fixed work route.
  WEV=$(jq -c --arg t "$TASK" 'select(.type == "route.decided") | .data
    | select(.decision.task == $t and .decision.decision == "route" and (.decision.attempt | contains(".worker.")))' "$RLOG" | tail -1)
  WDEC=$(jq -c '.decision // empty' <<<"${WEV:-{\}}")
  # Without ROUTE_FACTS, the review inherits the facts supplied for the work.
  [ -n "${ROUTE_FACTS:-}" ] || ROUTE_FACTS=$(jq -c '.request.facts // {} | del(.kind)' <<<"${WEV:-{\}}")
  AUTHOR=$(uv run -q --frozen --project /home/omegahive/repos/hive-route python -c '
import json, sys
from hiveroute.table import load_table
t = load_table(sys.argv[1])
d = json.loads(sys.argv[2]) if sys.argv[2] else {}
rid = d.get("route_id") or t.data["fixed"]["work"]
print(json.dumps({"family": t.routes[rid]["family"], "tier": d.get("tier") or t.routes[rid]["tier"]}))' \
    "$TABLE" "$WDEC")
fi
REQ=$(jq -n --arg t "$TASK" --arg a "$ATTEMPT" --arg r "$REASON" --arg k "$KIND" \
  --argjson facts "${ROUTE_FACTS:-{\}}" --argjson hint "${ROUTE_HINT:-null}" \
  --argjson author "$AUTHOR" --argjson history "$HISTORY" \
  '{task: $t, attempt: $a, reason: $r, facts: ({kind: $k} + $facts), tools_needed: true}
   + (if $hint then {hint: $hint} else {} end) + (if $author then {author: $author} else {} end)
   + (if ($history | length) > 0 then {history: $history} else {} end)')
# The task's text for the scorer: the kickoff and the order files it names.
{ cat KICKOFF.md; for f in $(grep -o "$DIR/workspace/[^ )\`]*/orders/[^ )\`]*\.md" KICKOFF.md | sort -u); do
    [ -f "$f" ] && { echo; echo "--- $f"; cat "$f"; }; done; } > route-task.md
RC=0; DEC=$(printf '%s' "$REQ" | hr decide "$TABLE" - --sources "$SOURCES" --log "$RLOG" --task-text route-task.md) || RC=$?
# Summaries of new log entries go to the record (amendment A1) as the router's actor, in the
# background, once that actor is registered; output in $ROOT/route-record.log.
RKEY=$HOME/.config/hive/agents/router.key
if [ -f "$RKEY" ] && HIVE_URL=http://127.0.0.1:8470 HIVE_ID=1-hive HIVE_KEY_FILE=$RKEY hive actors 2>/dev/null \
    | jq -e 'any(.[]; .id == "router")' >/dev/null 2>&1; then
  ( HIVE_URL=http://127.0.0.1:8470 HIVE_ID=1-hive HIVE_KEY_FILE=$RKEY HIVE_VIA=hive-route:router \
      nohup uv run -q --frozen --project /home/omegahive/repos/hive-route hive-route record "$RLOG" \
      >> "$ROOT/route-record.log" 2>&1 & )
fi
[ "$RC" -eq 0 ] || {
  echo "router: no route for this attempt" >&2
  printf '%s\n' "$DEC" | jq -c '{decision, wait_until, reasons: [.reasons[] | .rule + ": " + .note], rejected}' >&2
  exit 3; }
HARNESS=$(jq -r .harness <<<"$DEC") MODEL=$(jq -r .model <<<"$DEC") EFFORT=$(jq -r '.effort // empty' <<<"$DEC")
echo "route: $(jq -r '"\(.route_id) (\(.model)\(if .effort then ", " + .effort else "" end)), tier \(.tier), mode \(.mode)"' <<<"$DEC")"
jq -c '{attempt, route_id, tier, pool}' <<<"$DEC" >> "$HIST"

# Output names: worker-<n>.jsonl for Claude workers and codex-<n>.log for Codex reviewers,
# as before routing; otherwise <role>-<n>.<jsonl|log>.
case "$ROLE:$HARNESS" in
  worker:claude-code) OUT=$DIR/worker-$n.jsonl ;;
  reviewer:codex)     OUT=$DIR/codex-$n.log ;;
  *:claude-code)      OUT=$DIR/$PREFIX-$n.jsonl ;;
  *)                  OUT=$DIR/$PREFIX-$n.log ;;
esac
printf '%s' "$DEC" | hr manifest - --cwd "$DIR" --output "$OUT" > "$DIR/$PREFIX.$n.attempt.json"

cat > "$DIR/hive.env" <<EOF
export HIVE_URL=http://127.0.0.1:8470 HIVE_ID=1-hive HIVE_KEY_FILE=$KEY HIVE_VIA=$HARNESS:$ACTOR
export HIVEPIN_REPOSITORY_REGISTRY_PATH=$DIR/registry.json
export PATH=/home/omegahive/.local/bin:\$PATH
EOF

# ---- launch ---------------------------------------------------------------------------
# Reviews are capped at 90 minutes; work is not.
CAP=(); [ "$ROLE" = reviewer ] && CAP=(timeout --kill-after=30s 90m)
# A via_gateway route (e.g. SingularityCompute) goes through the hive gateway (deploy/gateway-up.sh),
# asking for the route's alias, its route_id.
GW=(); GWENV=()
if [ "$(jq -r '.via_gateway // false' <<<"$DEC")" = true ]; then
  GWURL=http://127.0.0.1:4000
  HIVE_GATEWAY_KEY=$(sed -n 's/^HIVE_GATEWAY_KEY=//p' "$HOME/.config/hive/gateway.env")
  MODEL=$(jq -r .route_id <<<"$DEC")
  case "$HARNESS" in
    claude-code) GWENV=(ANTHROPIC_BASE_URL="$GWURL" ANTHROPIC_AUTH_TOKEN="$HIVE_GATEWAY_KEY" ANTHROPIC_API_KEY=) ;;
    codex) GWENV=(HIVE_GATEWAY_KEY="$HIVE_GATEWAY_KEY")
           GW=(-c 'model_providers.hivegw.name="hive gateway"' -c "model_providers.hivegw.base_url=\"$GWURL/v1\""
               -c 'model_providers.hivegw.env_key="HIVE_GATEWAY_KEY"' -c 'model_providers.hivegw.wire_api="responses"'
               -c 'model_provider="hivegw"') ;;
  esac
fi
case "$HARNESS" in
  claude-code)
    # Print mode, not --bg: nothing can wait on a permission prompt overnight.
    # A refused action is returned to the agent, which takes another route.
    nohup env "${GWENV[@]}" "${CAP[@]}" claude -p --model "$MODEL" ${EFFORT:+--effort "$EFFORT"} --permission-mode auto \
      --output-format stream-json --verbose "$(cat KICKOFF.md)" > "$OUT" 2> "${OUT%.*}.err" &
    echo "pid:$! log $OUT" ;;
  codex)
    nohup env "${GWENV[@]}" "${CAP[@]}" codex exec --approve-for-me --skip-git-repo-check --cd "$DIR" "${GW[@]}" \
      -m "$MODEL" ${EFFORT:+-c model_reasoning_effort="$EFFORT"} \
      --add-dir /home/omegahive/repos/hive-workspace.git --output-last-message "$DIR/codex-last-message.md" \
      - < KICKOFF.md > "$OUT" 2>&1 &
    echo "codex pid $! log $OUT" ;;
  *) echo "router chose harness $HARNESS, which this launcher can't start" >&2; exit 3 ;;
esac
