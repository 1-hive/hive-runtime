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
# The container runs as OS user hive-cos (agent-users.sh). It holds only:
# - cos's record key and git key, and the Claude token, as Podman secrets;
# - ~/work/1hive/cos, mounted at /cos: its workspace clone, its Claude Code state, and
#   CLAUDE.md (docs/chief-of-staff.md);
# - the record, model gateway and sshd, through agent-ports.py's sockets.
# The registry's file:// fetch URLs reach the host's repositories over sshd, as cos's git does
# (hive-pin refuses a user name in a registry URL, so git rewrites them instead).
# Its MCP server `hive` is hive-mcp.py, installed in the image.
set -euo pipefail
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/paths.sh"
U=hive-cos NAME=cos
# HIVE_CONTAINER_RUNTIME=podman (1-hive: the agent user's rootless Podman, keys as secrets) or docker
# (vm2: plain docker as this user, keys bind-mounted read-only, the Claude token from an env file).
RT=${HIVE_CONTAINER_RUNTIME:-podman}
if [ "$RT" = docker ]; then P=(docker); else P=(sudo -n -u "$U" /usr/local/bin/hive-agent-podman); fi
D=$HIVE_WORK_DIR/cos A=$HIVE_CONFIG_DIR/agents
MODEL=${COS_MODEL:-claude-opus-5-5}
CLAUDE="claude --model $MODEL --permission-mode auto --mcp-config /cos/mcp.json"

setup() {
  [ "$RT" = docker ] || id "$U" >/dev/null 2>&1 || { echo "no OS user $U: sudo $RUNTIME_DIR/agent-users.sh cos" >&2; exit 2; }
  mkdir -p "$D/chat" "$D/.claude-state" "$D/followups"
  [ -d "$D/workspace/.git" ] || git clone -q "$HIVE_WORKSPACE_REPO" "$D/workspace"
  cp "${COS_ROLE_DOC:-$RUNTIME_DIR/docs/chief-of-staff.md}" "$D/CLAUDE.md"   # the role: 1-hive's, or e.g. docs/chief-of-staff-proto-hive.md
  printf '%s\n' '{"mcpServers": {"hive": {"command": "/opt/hive-mcp/venv/bin/python", "args": ["/opt/hive-mcp/hive-mcp.py"]}}}' > "$D/mcp.json"
  jq '.repositories.workspace.local_path = "/cos/workspace"' "$HIVE_DEPLOY_DIR/registry.json" > "$D/registry.json"
  # Files cos created are its own (and already carry the default ACL): skip those.
  setfacl -R -m "u:$U:rwX,d:u:$U:rwX,d:u:$USER:rwX" "$D" 2>/dev/null || true
  if [ "$RT" = docker ]; then
    # Docker has no secrets outside swarm: mount the key files read-only, pass the token from an env file.
    CREDS=(-v "$A/cos.key:/keys/cos.key:ro" -v "$A/cos.ssh:/root/.ssh/id_ed25519:ro"
           -v "$A/known_hosts:/root/.ssh/known_hosts:ro" --env-file "$D/.claude-token.env")
    ( umask 077; printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\n' "$(cat "$HIVE_CONFIG_DIR/claude-oauth-token")" > "$D/.claude-token.env" )
    NET=(--add-host host.docker.internal:host-gateway)
    return
  fi
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
  secret claude-token "$HIVE_CONFIG_DIR/claude-oauth-token"
  CREDS=(--secret "cos.key,type=mount,target=/keys/cos.key,mode=0400"
         --secret "cos.ssh,type=mount,target=/root/.ssh/id_ed25519,mode=0400"
         --secret "cos.known-hosts,type=mount,target=/root/.ssh/known_hosts,mode=0444"
         --secret "cos.claude-token,type=env,target=CLAUDE_CODE_OAUTH_TOKEN")
  NET=(--network slirp4netns --group-add keep-groups)
}

case "${1:-}" in
  run)
    setup
    "${P[@]}" rm -f "$NAME" >/dev/null 2>&1 || true
    exec "${P[@]}" run --rm --name "$NAME" --init "${NET[@]}" \
      -v "$HIVE_WORK_DIR/.ports:/run/hive-ports:ro" \
      -v "$D:/cos" -w /cos "${CREDS[@]}" \
      -e HIVE_URL=http://127.0.0.1:8470 -e "HIVE_ID=$HIVE_ID" -e HIVE_KEY_FILE=/keys/cos.key \
      -e HIVE_VIA=claude-code:cos -e HIVEPIN_REPOSITORY_REGISTRY_PATH=/cos/registry.json \
      -e CLAUDE_CONFIG_DIR=/cos/.claude-state -e TZ=CST6 \
      -e GIT_CONFIG_COUNT=3 -e "GIT_CONFIG_KEY_0=url.ssh://$USER@127.0.0.1$HIVE_REPOS_DIR/.insteadOf" \
      -e "GIT_CONFIG_VALUE_0=$HIVE_REPOS_DIR/" -e GIT_CONFIG_KEY_1=safe.directory -e 'GIT_CONFIG_VALUE_1=*' \
      -e "GIT_CONFIG_KEY_2=url.ssh://$USER@127.0.0.1$HIVE_REPOS_DIR/.insteadOf" \
      -e "GIT_CONFIG_VALUE_2=file://$HIVE_REPOS_DIR/" \
      -e GIT_AUTHOR_NAME=cos -e "GIT_AUTHOR_EMAIL=cos@$HIVE_ID.invalid" \
      -e GIT_COMMITTER_NAME=cos -e "GIT_COMMITTER_EMAIL=cos@$HIVE_ID.invalid" \
      localhost/1hive-agent:latest bash -c \
      "tmux new-session -d -s cos -x 200 -y 50 -c /cos '$CLAUDE --continue || $CLAUDE'
       while tmux has-session -t cos 2>/dev/null; do sleep 15; done" ;;
  attach)
    exec "${P[@]}" exec -it "$NAME" tmux attach -t cos ;;
  chat)
    msg=$(cat)
    [ -n "$msg" ] || exit 0
    # Continue the chat conversation; the first message starts it. A turn is stopped
    # inside the container after 15 minutes (killing the host side leaves it running),
    # and a stopped turn isn't retried as a new conversation.
    T="timeout -k 10 900"
    rc=0
    printf '%s' "$msg" | "${P[@]}" exec -i -w /cos/chat "$NAME" $T $CLAUDE -p --continue 2>/dev/null || rc=$?
    if [ "$rc" = 124 ] || [ "$rc" = 137 ]; then
      echo "(stopped after 15 minutes without a reply)"
    elif [ "$rc" != 0 ]; then
      printf '%s' "$msg" | "${P[@]}" exec -i -w /cos/chat "$NAME" $T $CLAUDE -p
    fi ;;
  stop)
    "${P[@]}" stop -t 10 "$NAME" ;;
  *) sed -n '2,10p' "$0" >&2; exit 2 ;;
esac
