#!/usr/bin/env bash
# Give the agents of an OpenClaw gateway a Claude subscription as a second Anthropic account (proto-hive's pop-os:
# protocosmopopos = agent main, and the agent ProtoCosmo2's Iter calls). OpenClaw moves to the next account when
# one hits a billing or usage limit; the subscription's usage limits apply. As root, reading it from git:
#
#   sudo git -C /opt/hive/hive-runtime show v0.5.47:adapters/openclaw/add-claude-account.sh | sudo bash -s [MODE]
#
# MODE: add (default: after the agents' own Anthropic keys), first (the subscription first), off (remove it),
# status (accounts and cooldowns, no secrets). add/first ask for the token (hidden); make one on any machine logged
# in to the subscription with `claude setup-token` (sk-ant-oat01-...). It goes to OpenClaw on stdin only.
# Every agent of the gateway that already has an Anthropic account gets it (GATEWAY_AGENTS="a b" to choose).
# No restart: the next model call picks it up.
set -euo pipefail
MODE=${1:-add}
case "$MODE" in add|first|off|status) ;; *) echo "mode: add | first | off | status" >&2; exit 2 ;; esac
[ "$(id -u)" = 0 ] || { echo "run it with sudo" >&2; exit 2; }
UNIT=${GATEWAY_UNIT:-openclaw-agent.service} PROFILE=anthropic:andy
PID=$(systemctl show -p MainPID --value "$UNIT")
[ -n "$PID" ] && [ "$PID" != 0 ] || { echo "$UNIT isn't running" >&2; exit 1; }
OWNER=$(stat -c %U "/proc/$PID")
NODE=$(readlink -f "/proc/$PID/exe")
mapfile -t ENVV < <(tr '\0' '\n' < "/proc/$PID/environ" | grep -E '^(HOME|PATH|OPENCLAW_[A-Z_]+|TMPDIR)=')
DIST=$(printf '%s\n' "${ENVV[@]}" | sed -n 's/^OPENCLAW_DIST=//p')
if [ -n "$DIST" ] && [ -f "$(dirname "$DIST")/openclaw.mjs" ]; then
  OC=("$NODE" "$(dirname "$DIST")/openclaw.mjs")
else
  OC=("$(sudo -u "$OWNER" -H bash -lc 'command -v openclaw' </dev/null)")
fi
oc() { sudo -u "$OWNER" env "${ENVV[@]}" timeout 150 "${OC[@]}" "$@"; }
ids() {  # $1 agent: its Anthropic profile ids, from the secret-free JSON listing
  oc models auth list --agent "$1" --provider anthropic --json </dev/null 2>/dev/null | python3 -c '
import json, sys
try: d = json.load(sys.stdin)
except Exception: sys.exit(0)
print(" ".join(p.get("id", "") for p in d.get("profiles", []) if p.get("id")))'
}
STATE=$(printf '%s\n' "${ENVV[@]}" | sed -n 's/^OPENCLAW_STATE_DIR=//p')
[ -n "$STATE" ] || STATE="$(getent passwd "$OWNER" | cut -d: -f6)/.openclaw"
AGENTS=${GATEWAY_AGENTS:-}
if [ -z "$AGENTS" ]; then
  for d in "$STATE"/agents/*/; do
    a=$(basename "$d")
    [ -n "$(ids "$a")" ] && AGENTS="$AGENTS $a"
  done
fi
[ -n "$AGENTS" ] || { echo "no agent of $UNIT has an Anthropic account" >&2; exit 1; }
echo "gateway $UNIT (user $OWNER); agents:$AGENTS"
T=
if [ "$MODE" = add ] || [ "$MODE" = first ]; then
  read -rsp "Claude token (input hidden): " T < /dev/tty; echo
  T=$(printf '%s' "$T" | tr -d '[:space:]')
  case "$T" in sk-ant-*) ;; *) echo "that doesn't look like a Claude token (sk-ant-...); nothing stored" >&2; exit 1 ;; esac
fi
for A in $AGENTS; do
  echo "== $A"
  case "$MODE" in
    off)
      oc models auth logout "$PROFILE" --agent "$A" --yes </dev/null || true
      oc models auth order clear --agent "$A" --provider anthropic </dev/null ;;
    add|first)
      printf '%s' "$T" | oc models auth paste-token --agent "$A" --provider anthropic --profile-id "$PROFILE" >/dev/null
      O=$(ids "$A" | tr ' ' '\n' | grep -vxF "$PROFILE" | tr '\n' ' ')
      [ "$MODE" = first ] && ORDER="$PROFILE $O" || ORDER="$O $PROFILE"
      # shellcheck disable=SC2086
      oc models auth order set --agent "$A" --provider anthropic $ORDER </dev/null
      echo "order: $ORDER" ;;
  esac
  oc models auth list --agent "$A" --provider anthropic </dev/null | sed -n '/^Profiles:/,$p'
done
T=
