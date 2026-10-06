#!/usr/bin/env bash
# Prepare a task folder for one actor and launch its session.
# Minimal stand-in for the Phase E launcher. Agents run on the host or, with the container
# runtime (ROUTE_RUNTIME, default deploy/runtime), each in its own rootless Podman container.
#
#   launch-task.sh worker   <task-id> <actor-id> <kickoff-file> [base-branch]
#   launch-task.sh reviewer <task-id> <actor-id> <kickoff-file>
#
# worker:   in ~/work/1hive/<task-id>, mtg-player on a new branch hive/<task-id> from
#           base-branch (default main). The commit the branch starts from is recorded once in
#           base.json ({"mtg-player": "<sha>"}): the worker pins it as the result's base.
#           Re-running for an existing folder relaunches in it (a restart, same base).
# reviewer: in ~/work/1hive/<task-id>-review, mtg-player on hive/<task-id>. The reviewer
#           reviews base..code from the result's pins (tools/result-refs.sh), not the branch.
#
# The kickoff file may use {DIR} for the task folder; it is copied to KICKOFF.md.
#
# The router (hive-route) picks the harness, model and effort for every attempt; the
# route block below. Optional environment for the router (ROUTING.md §4):
#   ROUTE_FACTS       JSON object of task facts (default: the order's 'Route facts:' line, below), e.g. '{"specification":"explicit",
#                     "verification":"independent","scope":"few","consequence":"reversible"}'
#   ROUTE_HINT        JSON {"tier": ..., "reason": ...}: raises the tier, never lowers it.
#                     For a reviewer, an order line "Review tier: strong" (or standard) sets it,
#                     so a high-stakes task's reviews ask for a higher tier than the author's.
#   ROUTE_LAST_CLASS  failure class of this role's previous attempt on the task (§5):
#                     outage, capacity, truncated, missing_info, failed_check, stalled,
#                     indeterminate, interrupted, checkpoint (the worker asked to be routed again;
#                     the supervisor passes it with the facts or hint the checkpoint set)
#   ROUTE_REASON      reassign, to move off the previous route
#   EXTRA_REPOS       more registered repositories the task changes (e.g. "mtg-colosseo"), each
#                     mirrored at ~/repos/<name>.git: a worker gets <name> on hive/<task> from main
#                     (its base recorded in base.json), a reviewer gets hive/<task>.
#   ROUTE_RUNTIME     host or container (default: deploy/runtime). The container runtime needs the
#                     agent image (deploy/agent/build.sh), git keys (tools/git/install.sh) and, for
#                     Claude Code, ~/.config/hive/claude-oauth-token (`claude setup-token`).
#   ROUTE_OVERRIDE    JSON {"route_id": ..., "reason": ..., "by": "operator"}: an operator's
#                     explicit choice (RT1), e.g. a same-family review when no other family has
#                     capacity. Only on the operator's instruction; the reason says which.
# Claude Code runs in print mode with auto permissions; Codex runs `exec`.
set -euo pipefail
ROLE=$1 TASK=$2 ACTOR=$3 KICKOFF=$4 BASE=${5:-main}
# Ids go into paths, a Codex config override and an HTTP header: only the record's id syntax.
ID='^[a-z0-9][a-z0-9._-]{0,63}$'
[[ "$TASK" =~ $ID && "$ACTOR" =~ $ID && "$TASK" != *..* && "$ACTOR" != *..* ]] \
  || { echo "task and actor ids must match $ID" >&2; exit 2; }
case "$ROLE" in worker|reviewer) ;; *) echo "role must be worker or reviewer" >&2; exit 2 ;; esac
[[ "$BASE" =~ ^[A-Za-z0-9._/-]+$ && "$BASE" != -* ]] || { echo "bad base branch" >&2; exit 2; }
ROOT=$HOME/work/1hive
DIR=$ROOT/$TASK; [ "$ROLE" = reviewer ] && DIR=$ROOT/$TASK-review
SEED_BUILD=${SEED_BUILD:-$ROOT/hbp-1-feasibility/mtg-player/adapters/xmage-external-seat/.build}
KEY=$HOME/.config/hive/agents/$ACTOR.key
[ -f "$KEY" ] || { echo "no key for $ACTOR" >&2; exit 2; }
# Where the attempt runs: `host`, as the operator's user, or `container`: its own rootless Podman
# container (PLAN Phase E 1; SPEC §6.6), holding only this task's folder, this actor's record and
# git keys, and the harness's credentials. Default: deploy/runtime. Reported to the router.
RUNTIME=${ROUTE_RUNTIME:-$(cat /home/omegahive/repos/1-hive/deploy/runtime 2>/dev/null || echo host)}
case "$RUNTIME" in
  host) H=127.0.0.1 ;;
  container)   # from a container, the host's loopback (record, model gateway, sshd) is 10.0.2.2
    H=10.0.2.2
    [ -f "$HOME/.config/hive/agents/$ACTOR.ssh" ] && [ -f "$HOME/.config/hive/agents/known_hosts" ] \
      || { echo "no git key for $ACTOR: the operator runs tools/git/install.sh" >&2; exit 2; }
    case "$ACTOR" in reviewer.codex.*) ;; *)
      grep -q '[^[:space:]]' "$HOME/.config/hive/claude-oauth-token" 2>/dev/null \
        || { echo "no ~/.config/hive/claude-oauth-token: the operator runs 'claude setup-token' and saves it there" >&2; exit 2; } ;;
    esac ;;
  *) echo "unknown runtime $RUNTIME (host or container)" >&2; exit 2 ;;
esac

if [ ! -d "$DIR" ]; then
  mkdir -p "$DIR"
  git clone -q /home/omegahive/repos/hive-workspace.git "$DIR/workspace"
  if [ "$ROLE" = worker ]; then
    git clone -q -b "$BASE" /home/omegahive/repos/mtg-player.git "$DIR/mtg-player"
    git -C "$DIR/mtg-player" checkout -q -b "hive/$TASK"
    # The result's base pin: where this task's change starts (hive-record SPEC §24.7).
    jq -n --arg c "$(git -C "$DIR/mtg-player" rev-parse HEAD)" '{"mtg-player": $c}' > "$DIR/base.json"
  else
    # The task's code may live in another repo (e.g. the arena); then review mtg-player main.
    git clone -q -b "hive/$TASK" /home/omegahive/repos/mtg-player.git "$DIR/mtg-player" 2>/dev/null \
      || git clone -q /home/omegahive/repos/mtg-player.git "$DIR/mtg-player"
  fi
  for R in ${EXTRA_REPOS:-}; do
    [[ "$R" =~ $ID ]] || { echo "bad repo name $R" >&2; exit 2; }
    if [ "$ROLE" = worker ]; then
      git clone -q "/home/omegahive/repos/$R.git" "$DIR/$R"
      git -C "$DIR/$R" checkout -q -b "hive/$TASK"
      jq --arg r "$R" --arg c "$(git -C "$DIR/$R" rev-parse HEAD)" '. + {($r): $c}' "$DIR/base.json" > "$DIR/base.json.tmp"
      mv "$DIR/base.json.tmp" "$DIR/base.json"
    else
      git clone -q -b "hive/$TASK" "/home/omegahive/repos/$R.git" "$DIR/$R"
    fi
  done
  if [ -d "$SEED_BUILD" ]; then
    cp -a "$SEED_BUILD" "$DIR/mtg-player/adapters/xmage-external-seat/.build"
  fi
  python3 - "$DIR" ${EXTRA_REPOS:-} <<'EOF'
import json, sys
d = sys.argv[1]
reg = json.load(open("/home/omegahive/repos/1-hive/deploy/registry.json"))
for name in ["workspace", "mtg-player", *sys.argv[2:]]:
    reg["repositories"][name]["local_path"] = f"{d}/{name}"
json.dump(reg, open(f"{d}/registry.json", "w"), indent=2)
EOF
fi
cp /home/omegahive/repos/1-hive/docs/worker-contract.md "$DIR/WORKER-CONTRACT.md"
cp /home/omegahive/repos/1-hive/tools/result-refs.sh "$DIR/result-refs.sh"
sed "s#{DIR}#$DIR#g" "$KICKOFF" > "$DIR/KICKOFF.md"
git -C "$DIR/workspace" pull -q --ff-only || true
cd "$DIR"

# ---- route: ask the router which harness and model run this attempt ------------------
# Table deploy/route-table.yaml; pool usage and qualifications from deploy/route-sources.yaml.
# Every decision goes to $ROOT/route-log.jsonl (created in fixed mode); each attempt gets a
# manifest ($ROOT/.route/<folder>/<role>.<n>.attempt.json) so the router can check which model ran.
TABLE=/home/omegahive/repos/1-hive/deploy/route-table.yaml
SOURCES=/home/omegahive/repos/1-hive/deploy/route-sources.yaml
RLOG=$ROOT/route-log.jsonl
hr() { uv run -q --frozen --project /home/omegahive/repos/hive-route hive-route "$@"; }
[ -f "$RLOG" ] || hr mode fixed "$TABLE" --log "$RLOG" >/dev/null
# Earlier attempts' reported models against their routes: logs drift, demotes the route.
hr observe "$RLOG" "$ROOT/.route/*/*.attempt.json" --sources "$SOURCES" >&2 || true

# Attempt numbers count this role's earlier outputs, whatever harness wrote them.
if [ "$ROLE" = worker ]; then KIND=work; PREFIX=worker; OLD="worker-*.jsonl worker-*.log"
else KIND=review; PREFIX=review; OLD="codex-*.log review-*.jsonl review-*.log"; fi
n=$( (ls $OLD 2>/dev/null || true) | wc -l)
ATTEMPT=$TASK.$PREFIX.$n
REASON=${ROUTE_REASON:-new}; [ "$n" -gt 0 ] && [ -z "${ROUTE_REASON:-}" ] && REASON=restart

# This role's earlier attempts on the task, with their failure classes where known.
# Route state lives outside the agent's folder, so the agent doesn't edit what steers it.
RS=$ROOT/.route/$(basename "$DIR"); mkdir -p "$RS"
HIST=$RS/route-history.jsonl
[ -f "$HIST" ] || { [ -f "$DIR/route-history.jsonl" ] && cp "$DIR/route-history.jsonl" "$HIST"; touch "$HIST"; }
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
# A high-stakes order asks for a higher review tier: "Review tier: strong" in the order file.
if [ "$ROLE" = reviewer ] && [ -z "${ROUTE_HINT:-}" ]; then
  ORDERS=$(grep -o "projects/[^ )\`]*/orders/[^ )\`]*\.md" KICKOFF.md | sort -u | sed "s#^#$DIR/workspace/#")
  RT=$(grep -h -o -i -E '^Review tier: *(light|standard|strong)' $ORDERS /dev/null 2>/dev/null \
       | head -1 | sed -E 's/.*: *//' | tr 'A-Z' 'a-z' || true)   # no such line: no hint
  [ -n "$RT" ] && ROUTE_HINT=$(jq -nc --arg t "$RT" '{tier: $t, reason: "the order asks for this review tier"}')
fi
# A work order states the task's facts on a line of its own, e.g.
#   Route facts: specification=explicit verification=independent scope=few consequence=reversible leverage=0
# (any fact may be `unknown`). ROUTE_FACTS in the environment wins (restarts pass it). A new
# task without either is refused, so no task runs on the strong default because its facts were
# forgotten; ROUTE_FACTS=none launches it without facts on purpose.
if [ "$ROLE" = worker ] && [ -z "${ROUTE_FACTS:-}" ]; then
  ORDERS=$(grep -o "projects/[^ )\`]*/orders/[^ )\`]*\.md" KICKOFF.md | sort -u | sed "s#^#$DIR/workspace/#")
  FL=$(grep -h -i -E '^Route facts:' $ORDERS /dev/null 2>/dev/null | head -1 | sed -E 's/^[^:]*: *//' || true)
  if [ -n "$FL" ]; then
    ROUTE_FACTS=$(python3 - "$FL" <<'EOF'
import json, sys
ok = {"specification": ("explicit", "partial", "goal_only"), "verification": ("independent", "weak", "none"),
      "scope": ("single", "few", "many"), "consequence": ("reversible", "costly")}
facts = {}
for item in sys.argv[1].replace(",", " ").split():
    k, _, v = item.partition("=")
    if v == "unknown" and (k in ok or k == "leverage"):
        continue
    if k in ok and v in ok[k]:
        facts[k] = v
    elif k == "leverage" and v.isdigit():
        facts[k] = int(v)
    else:
        sys.exit(f"Route facts: bad item {item!r}")
print(json.dumps(facts))
EOF
    ) || { echo "fix the order's Route facts line" >&2; exit 2; }
  elif [ "$REASON" = new ]; then
    echo "no route facts: add a 'Route facts:' line to the order (any fact may be unknown)," >&2
    echo "or set ROUTE_FACTS (ROUTE_FACTS=none to launch without facts)" >&2
    exit 2
  fi
fi
[ "${ROUTE_FACTS:-}" = none ] && ROUTE_FACTS='{}'
# runtime: where the attempt runs, so the router's evidence from before and after Phase E
# (containers, a separate OS user) stays apart. Today agents run on the host as the operator's user.
REQ=$(jq -n --arg t "$TASK" --arg a "$ATTEMPT" --arg r "$REASON" --arg k "$KIND" --arg rt "$RUNTIME" \
  --argjson facts "${ROUTE_FACTS:-{\}}" --argjson hint "${ROUTE_HINT:-null}" \
  --argjson author "$AUTHOR" --argjson history "$HISTORY" --argjson override "${ROUTE_OVERRIDE:-null}" \
  '{task: $t, attempt: $a, reason: $r, runtime: $rt, facts: ({kind: $k} + $facts), tools_needed: true}
   + (if $hint then {hint: $hint} else {} end) + (if $author then {author: $author} else {} end)
   + (if $override then {override: $override} else {} end)
   + (if ($history | length) > 0 then {history: $history} else {} end)')
# The task's text for the scorer: the kickoff and the order files it names.
{ cat KICKOFF.md; for f in $(grep -o "$DIR/workspace/[^ )\`]*/orders/[^ )\`]*\.md" KICKOFF.md | sort -u); do
    [ -f "$f" ] && { echo; echo "--- $f"; cat "$f"; }; done; } > route-task.md
RC=0; DEC=$(printf '%s' "$REQ" | hr decide "$TABLE" - --sources "$SOURCES" --log "$RLOG" --task-text route-task.md) || RC=$?
# Summaries of new log entries go to the record (amendment A1) as the router's actor, in the
# background, once that actor is registered; output in $ROOT/route-record.log.
RKEY=$HOME/.config/hive/agents/router.key
# Only the deployment's own log is ever synced (a copy of this block run on a scratch log once
# posted test entries to the record).
if [ "$RLOG" = "$HOME/work/1hive/route-log.jsonl" ] && [ -f "$RKEY" ] && HIVE_URL=http://127.0.0.1:8470 HIVE_ID=1-hive HIVE_KEY_FILE=$RKEY hive actors 2>/dev/null \
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

# The record's SPEC §6.2: an actor re-declares when its configuration changes. When the
# router picks a different harness or model than the actor's current declaration, the
# launcher re-declares it, signed with the actor's own key, before the attempt starts.
ROUTE_DESC=$(jq -r '"\(.route_id): \(.model)\(if .effort then " (" + .effort + ")" else "" end)"' <<<"$DEC")
CUR=$(HIVE_URL=http://127.0.0.1:8470 HIVE_ID=1-hive HIVE_KEY_FILE=$KEY hive actors 2>/dev/null \
  | jq -c --arg a "$ACTOR" '.[] | select(.id == $a) | .declaration' 2>/dev/null || true)
if [ -n "$CUR" ] && [ "$(jq -r '.harness + "|" + .model_route' <<<"$CUR")" != "$HARNESS|$ROUTE_DESC" ]; then
  NEWDECL=$(jq -c --arg a "$ACTOR" --arg h "$HARNESS" --arg m "$ROUTE_DESC" \
    '{actor_id: $a, declaration: (. + {harness: $h, model_route: $m})}' <<<"$CUR")
  HIVE_URL=http://127.0.0.1:8470 HIVE_ID=1-hive HIVE_KEY_FILE=$KEY HIVE_VIA=launcher:$ACTOR \
    hive emit actor.declared --data "$NEWDECL" >/dev/null 2>&1 \
    && echo "declared: $ACTOR now $HARNESS, $ROUTE_DESC" \
    || echo "warning: couldn't re-declare $ACTOR as $HARNESS, $ROUTE_DESC" >&2
fi

# An attempt on a metered route (pay per call) gets its own gateway budget, attempt:<id>, so the
# pool's per-attempt limit stops it running over (the router only decides whether it fits).
AB=$(printf '%s' "$DEC" | hr attempt-budget "$TABLE" -)
if [ -n "$AB" ]; then
  GWMASTER=$(sed -n 's/^HIVE_GATEWAY_KEY=//p' "$HOME/.config/hive/gateway.env")
  curl -s -m 20 -X POST http://127.0.0.1:4000/tag/new -H "Authorization: Bearer $GWMASTER" \
    -H 'Content-Type: application/json' -d "$AB" | jq -e '.tag // .message' >/dev/null 2>&1 \
    && echo "budget: $(jq -r '.name + " $" + (.max_budget|tostring)' <<<"$AB")" \
    || { echo "couldn't create the attempt's gateway budget; not launching" >&2; exit 3; }
  unset GWMASTER
fi

# Output names: worker-<n>.jsonl for Claude workers and codex-<n>.log for Codex reviewers,
# as before routing; otherwise <role>-<n>.<jsonl|log>.
case "$ROLE:$HARNESS" in
  worker:claude-code) OUT=$DIR/worker-$n.jsonl ;;
  reviewer:codex)     OUT=$DIR/codex-$n.log ;;
  *:claude-code*)     OUT=$DIR/$PREFIX-$n.jsonl ;;
  *)                  OUT=$DIR/$PREFIX-$n.log ;;
esac
printf '%s' "$DEC" | hr manifest - --cwd "$DIR" --output "$OUT" > "$RS/$PREFIX.$n.attempt.json"

cat > "$DIR/hive.env" <<EOF
export HIVE_URL=http://$H:8470 HIVE_ID=1-hive HIVE_KEY_FILE=$KEY HIVE_VIA=$HARNESS:$ACTOR
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
  GWURL=http://$H:4000
  # The worker key only calls models; the gateway's master key never reaches an agent.
  HIVE_GATEWAY_KEY=$(sed -n 's/^HIVE_WORKER_KEY=//p' "$HOME/.config/hive/gateway.env")
  MODEL=$(jq -r .route_id <<<"$DEC")
  case "$HARNESS" in
    # Spend in the gateway is tagged by task and attempt, as well as by pool, tier and route.
    claude-code|claude-code-allowlist) GWENV=(ANTHROPIC_BASE_URL="$GWURL" ANTHROPIC_AUTH_TOKEN="$HIVE_GATEWAY_KEY" ANTHROPIC_API_KEY=
                        ANTHROPIC_CUSTOM_HEADERS="x-litellm-tags: task:$TASK,attempt:$ATTEMPT") ;;
    codex) GWENV=(HIVE_GATEWAY_KEY="$HIVE_GATEWAY_KEY")
           GW=(-c 'model_providers.hivegw.name="hive gateway"' -c "model_providers.hivegw.base_url=\"$GWURL/v1\""
               -c "model_providers.hivegw.http_headers={\"x-litellm-tags\"=\"task:$TASK,attempt:$ATTEMPT\"}"
               -c 'model_providers.hivegw.env_key="HIVE_GATEWAY_KEY"' -c 'model_providers.hivegw.wire_api="responses"'
               -c 'model_provider="hivegw"') ;;
  esac
fi
ALLOWLIST=/home/omegahive/repos/1-hive/deploy/review-allowlist.json
if [ "$RUNTIME" = host ]; then
  RUN=(env "${GWENV[@]}" "${CAP[@]}")
  WSDIR=(--add-dir /home/omegahive/repos/hive-workspace.git)   # codex: the workspace remote
else
  A=$HOME/.config/hive/agents
  CNAME=hive-$(tr . - <<<"$ATTEMPT")
  # Clones' remotes are the bare repositories' host paths; in the container, git reaches them
  # over SSH as this actor (tools/git/hive-git-shell, whose pre-receive hook limits its pushes).
  CENV=(-e GIT_CONFIG_COUNT=1 -e "GIT_CONFIG_KEY_0=url.ssh://$USER@$H/home/omegahive/repos/.insteadOf"
        -e GIT_CONFIG_VALUE_0=/home/omegahive/repos/
        -e "GIT_AUTHOR_NAME=$ACTOR" -e "GIT_AUTHOR_EMAIL=$ACTOR@1-hive.invalid"
        -e "GIT_COMMITTER_NAME=$ACTOR" -e "GIT_COMMITTER_EMAIL=$ACTOR@1-hive.invalid")
  for kv in "${GWENV[@]}"; do CENV+=(-e "$kv"); done
  CRED=()
  case "$HARNESS" in
    claude-code*)
      # A long-lived token (`claude setup-token`), not the operator's own login, whose refresh
      # would race the host's.
      # Passed by name from this environment, so it isn't on a command line.
      if [ "${#GWENV[@]}" -eq 0 ]; then
        export CLAUDE_CODE_OAUTH_TOKEN; CLAUDE_CODE_OAUTH_TOKEN=$(cat "$HOME/.config/hive/claude-oauth-token")
        CRED=(-e CLAUDE_CODE_OAUTH_TOKEN)
      fi ;;
    codex) CRED=(-v "$HOME/.codex:/root/.codex") ;;   # the shared login, refreshed in place
  esac
  CAPC=(); [ "$ROLE" = reviewer ] && CAPC=(--timeout 5400)
  RUN=(podman run --rm --name "$CNAME" --init "${CAPC[@]}"
       --network slirp4netns:allow_host_loopback=true
       --device /dev/fuse --device /dev/net/tun
       --security-opt label=disable --security-opt seccomp=unconfined --security-opt 'unmask=/proc/*'
       -v "$DIR:$DIR" -w "$DIR" -v "$KEY:$KEY:ro"
       -v "$A/$ACTOR.ssh:/root/.ssh/id_ed25519:ro" -v "$A/known_hosts:/root/.ssh/known_hosts:ro"
       -v "$ALLOWLIST:$ALLOWLIST:ro" "${CRED[@]}" "${CENV[@]}" localhost/1hive-agent:latest)
  WSDIR=()
  echo "$CNAME" > "$RS/$PREFIX.$n.container"   # the supervisor stops it by name
fi
case "$HARNESS" in
  claude-code)
    # Print mode, not --bg: nothing can wait on a permission prompt overnight.
    # A refused action is returned to the agent, which takes another route.
    nohup "${RUN[@]}" claude -p --model "$MODEL" ${EFFORT:+--effort "$EFFORT"} --permission-mode auto \
      --output-format stream-json --verbose "$(cat KICKOFF.md)" > "$OUT" 2> "${OUT%.*}.err" &
    echo "$!" > "$RS/$PREFIX.$n.pid"   # the supervisor finds the attempt's process here
    echo "pid:$! log $OUT ($RUNTIME)" ;;
  claude-code-allowlist)
    # No model judges this route's actions (its own model would, in auto mode): a fixed allow list,
    # everything else denied (deploy/review-allowlist.json; table v14).
    nohup "${RUN[@]}" claude -p --model "$MODEL" ${EFFORT:+--effort "$EFFORT"} --permission-mode dontAsk \
      --settings "$ALLOWLIST" \
      --output-format stream-json --verbose "$(cat KICKOFF.md)" > "$OUT" 2> "${OUT%.*}.err" &
    echo "$!" > "$RS/$PREFIX.$n.pid"
    echo "pid:$! log $OUT ($RUNTIME)" ;;
  codex)
    nohup "${RUN[@]}" codex exec --approve-for-me --skip-git-repo-check --cd "$DIR" "${GW[@]}" \
      -m "$MODEL" ${EFFORT:+-c model_reasoning_effort="$EFFORT"} \
      "${WSDIR[@]}" --output-last-message "$DIR/codex-last-message.md" \
      - < KICKOFF.md > "$OUT" 2>&1 &
    echo "$!" > "$RS/$PREFIX.$n.pid"   # the supervisor finds the attempt's process here
    echo "codex pid $! log $OUT ($RUNTIME)" ;;
  *) echo "router chose harness $HARNESS, which this launcher can't start" >&2; exit 3 ;;
esac
