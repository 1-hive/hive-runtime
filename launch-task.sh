#!/usr/bin/env bash
# Prepare a task folder for one actor and launch its session.
# Minimal stand-in for the Phase E launcher: same steps, no containers yet.
#
#   launch-task.sh worker   <task-id> <actor-id> <kickoff-file> [base-branch]
#   launch-task.sh reviewer <task-id> <actor-id> <kickoff-file>
#
# worker:   Claude Code (print mode, auto permissions) in ~/work/1hive/<task-id>,
#           mtg-player on a new branch hive/<task-id> from base-branch (default main).
#           Re-running for an existing folder relaunches in it (a restart).
# reviewer: Codex (exec) in ~/work/1hive/<task-id>-review, mtg-player on hive/<task-id>.
#
# The kickoff file may use {DIR} for the task folder; it is copied to KICKOFF.md.
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
HARNESS=claude-code; [ "$ROLE" = reviewer ] && HARNESS=codex
cat > "$DIR/hive.env" <<EOF
export HIVE_URL=http://127.0.0.1:8470 HIVE_ID=1-hive HIVE_KEY_FILE=$KEY HIVE_VIA=$HARNESS:$ACTOR
export HIVEPIN_REPOSITORY_REGISTRY_PATH=$DIR/registry.json
export PATH=/home/omegahive/.local/bin:\$PATH
EOF
sed "s#{DIR}#$DIR#g" "$KICKOFF" > "$DIR/KICKOFF.md"
git -C "$DIR/workspace" pull -q --ff-only || true

cd "$DIR"
if [ "$ROLE" = worker ]; then
  # Print mode, not --bg: nothing can wait on a permission prompt overnight.
  # A refused action is returned to the agent, which takes another route.
  n=$( (ls worker*.jsonl 2>/dev/null || true) | wc -l)
  nohup claude -p --permission-mode auto --output-format stream-json --verbose \
    "$(cat KICKOFF.md)" > "$DIR/worker-$n.jsonl" 2> "$DIR/worker-$n.err" &
  echo "pid:$! log $DIR/worker-$n.jsonl"
else
  n=$( (ls codex*.log 2>/dev/null || true) | wc -l)
  nohup timeout --kill-after=30s 90m codex exec --approve-for-me --skip-git-repo-check --cd "$DIR" \
    --add-dir /home/omegahive/repos/hive-workspace.git --output-last-message "$DIR/codex-last-message.md" \
    - < KICKOFF.md > "$DIR/codex-$n.log" 2>&1 &
  echo "codex pid $! log $DIR/codex-$n.log"
fi
