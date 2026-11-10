#!/usr/bin/env bash
# Load a task folder's skills from the hive's skills registry (One Hive R4, hive-skills).
#
#   load-skills.sh <task-folder> <role> <harness>
#
# Installs the registered skills for <role> on <harness> into <folder>/.agents/skills (the Agent Skills location
# Codex, Copilot and Cursor read), links <folder>/.claude/skills to it (Claude Code's), and points the folder's
# WORKER-CONTRACT.md, review-wait.py and result-refs.sh at the hive-worker skill, so every kickoff, contract and
# restart context that names them reads the registered version. Loading again on a restart installs the version
# registered now and restores an installed copy that was edited (hive-skills SPEC §4).
#
# Prints one JSON line: hive-skill's load report ({set_digest, skills, installed, updated, restored, removed, ...}),
# for the launcher to keep with the attempt. A refusal (an expired skill, a conflict, a pin that doesn't verify)
# exits non-zero and changes nothing installed: the task must not run without the skills it was meant to have.
#
# Registry: HIVE_SKILLS_REGISTRY, else $HIVE_DEPLOY_DIR/skills.json. Without one (a hive that hasn't adopted R4),
# the runtime's own skills/hive-worker is copied in, unregistered, as before R4: {"registered": false}.
set -euo pipefail
DIR=$1 ROLE=$2 HARNESS=${3%-allowlist}   # claude-code-allowlist is Claude Code with a fixed allow list
HERE=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
REG=${HIVE_SKILLS_REGISTRY:-${HIVE_DEPLOY_DIR:+$HIVE_DEPLOY_DIR/skills.json}}
SK=$DIR/.agents/skills
mkdir -p "$DIR/.agents" "$DIR/.claude"
if [ -n "$REG" ] && [ -f "$REG" ]; then
  OUT=$("${HIVE_SKILL:-hive-skill}" --registry "$REG" --offline --json load "$SK" --role "$ROLE" --harness "$HARNESS") || {
    echo "skills: $(jq -r '.error | "\(.code): \(.message)"' <<<"$OUT" 2>/dev/null || echo "$OUT")" >&2; exit 3; }
else
  rm -rf "$SK/hive-worker" && mkdir -p "$SK" && cp -r "$HERE/skills/hive-worker" "$SK/hive-worker"
  OUT='{"registered": false, "skills": [{"name": "hive-worker"}]}'
fi
# .claude/skills is the same directory (a folder from before R4 had none).
[ -L "$DIR/.claude/skills" ] || { rm -rf "$DIR/.claude/skills"; ln -s ../.agents/skills "$DIR/.claude/skills"; }
if [ -f "$SK/hive-worker/SKILL.md" ]; then
  ln -sfn .agents/skills/hive-worker/SKILL.md "$DIR/WORKER-CONTRACT.md"
  ln -sfn .agents/skills/hive-worker/scripts/review-wait.py "$DIR/review-wait.py"
  ln -sfn .agents/skills/hive-worker/scripts/result-refs.sh "$DIR/result-refs.sh"
fi
echo "$OUT"
