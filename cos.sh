#!/usr/bin/env bash
# The chief of staff in its own container (1-hive PLAN, Phase E item 4; docs/chief-of-staff.md).
#
#   cos.sh run      the container, in the foreground: cos's Claude Code session in tmux
#                   (the 1-hive-cos user service runs this)
#   cos.sh attach   the operator's terminal on that session (`op cos`; detach: Ctrl-b d)
#   cos.sh chat     one message on stdin, cos's reply on stdout (the Telegram bridge's --chat-cmd).
#                   A separate, continuing conversation in /cos/chat, sharing the same record and files.
#   cos.sh stop
#
# The container runs as OS user hive-cos (deploy/agent-users.sh). It holds only:
# - cos's record key and git key, and the Claude token, as Podman secrets;
# - ~/work/1hive/cos, mounted at /cos: its workspace clone, its Claude Code state, and
#   CLAUDE.md (docs/chief-of-staff.md);
# - the record, model gateway and sshd, through tools/agent-ports.py's sockets.
# Its MCP server `hive` is tools/hive-mcp.py, installed in the image.
set -euo pipefail
U=hive-cos NAME=cos
P=(sudo -n -u "$U" /usr/local/bin/hive-agent-podman)
D=$HOME/work/1hive/cos A=$HOME/.config/hive/agents
REPO=/home/omegahive/repos/1-hive
MODEL=${COS_MODEL:-claude-opus-5-5}
CLAUDE="claude --model $MODEL --permission-mode auto --mcp-config /cos/mcp.json"

setup() {
  id "$U" >/dev/null 2>&1 || { echo "no OS user $U: sudo $REPO/deploy/agent-users.sh cos" >&2; exit 2; }
  mkdir -p "$D/chat" "$D/.claude-state"
  [ -d "$D/workspace/.git" ] || git clone -q /home/omegahive/repos/hive-workspace.git "$D/workspace"
  cp "$REPO/docs/chief-of-staff.md" "$D/CLAUDE.md"
  printf '%s\n' '{"mcpServers": {"hive": {"command": "/opt/hive-mcp/venv/bin/python", "args": ["/opt/hive-mcp/hive-mcp.py"]}}}' > "$D/mcp.json"
  jq '.repositories.workspace.local_path = "/cos/workspace"' "$REPO/deploy/registry.json" > "$D/registry.json"
  setfacl -R -m "u:$U:rwX,d:u:$U:rwX,d:u:$USER:rwX" "$D"
  local want; want=$(podman image inspect --format '{{.Id}}' localhost/1hive-agent:latest)
  if [ "$("${P[@]}" image inspect --format '{{.Id}}' localhost/1hive-agent:latest 2>/dev/null)" != "$want" ]; then
    podman save localhost/1hive-agent:latest | "${P[@]}" load -q >/dev/null
  fi
  secret() {   # piped: sudo (use_pty) doesn't pass a redirected file through as stdin
    "${P[@]}" secret rm "cos.$1" >/dev/null 2>&1 || true
    cat "$2" | "${P[@]}" secret create "cos.$1" - >/dev/null
  }
  secret key "$A/cos.key"
  secret ssh "$A/cos.ssh"
  secret known-hosts "$A/known_hosts"
  secret claude-token "$HOME/.config/hive/claude-oauth-token"
}

case "${1:-}" in
  run)
    setup
    "${P[@]}" rm -f "$NAME" >/dev/null 2>&1 || true
    exec "${P[@]}" run --rm --name "$NAME" --init --network slirp4netns \
      -v "$HOME/work/1hive/.ports:/run/hive-ports:ro" --group-add keep-groups \
      -v "$D:/cos" -w /cos \
      --secret "cos.key,type=mount,target=/keys/cos.key,mode=0400" \
      --secret "cos.ssh,type=mount,target=/root/.ssh/id_ed25519,mode=0400" \
      --secret "cos.known-hosts,type=mount,target=/root/.ssh/known_hosts,mode=0444" \
      --secret "cos.claude-token,type=env,target=CLAUDE_CODE_OAUTH_TOKEN" \
      -e HIVE_URL=http://127.0.0.1:8470 -e HIVE_ID=1-hive -e HIVE_KEY_FILE=/keys/cos.key \
      -e HIVE_VIA=claude-code:cos -e HIVEPIN_REPOSITORY_REGISTRY_PATH=/cos/registry.json \
      -e CLAUDE_CONFIG_DIR=/cos/.claude-state -e TZ=CST6 \
      -e GIT_CONFIG_COUNT=2 -e "GIT_CONFIG_KEY_0=url.ssh://$USER@127.0.0.1/home/omegahive/repos/.insteadOf" \
      -e GIT_CONFIG_VALUE_0=/home/omegahive/repos/ -e GIT_CONFIG_KEY_1=safe.directory -e 'GIT_CONFIG_VALUE_1=*' \
      -e GIT_AUTHOR_NAME=cos -e GIT_AUTHOR_EMAIL=cos@1-hive.invalid \
      -e GIT_COMMITTER_NAME=cos -e GIT_COMMITTER_EMAIL=cos@1-hive.invalid \
      localhost/1hive-agent:latest bash -c \
      "tmux new-session -d -s cos -x 200 -y 50 -c /cos '$CLAUDE --continue || $CLAUDE'
       while tmux has-session -t cos 2>/dev/null; do sleep 15; done" ;;
  attach)
    exec "${P[@]}" exec -it "$NAME" tmux attach -t cos ;;
  chat)
    msg=$(cat)
    [ -n "$msg" ] || exit 0
    # Continue the chat conversation; the first message starts it.
    printf '%s' "$msg" | "${P[@]}" exec -i -w /cos/chat "$NAME" $CLAUDE -p --continue 2>/dev/null \
      || printf '%s' "$msg" | "${P[@]}" exec -i -w /cos/chat "$NAME" $CLAUDE -p ;;
  stop)
    "${P[@]}" stop -t 10 "$NAME" ;;
  *) sed -n '2,10p' "$0" >&2; exit 2 ;;
esac
