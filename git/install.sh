#!/usr/bin/env bash
# Set up agents' git access over SSH (1-hive PLAN, Phase D item 3). Run by the operator:
# - the pre-receive hook in every bare repository under ~/repos;
# - an SSH key per agent (~/.config/hive/agents/<actor>.ssh), restricted in
#   ~/.ssh/authorized_keys to `hive-git-shell <actor>` (no shell, no forwarding);
# - ~/.config/hive/agents/known_hosts: the host's sshd as containers reach it (127.0.0.1, relayed
#   by agent-ports.py).
#   git/install.sh [--prune] [actor...]     (default: every agent key in ~/.config/hive/agents)
# --prune: also remove the git access of every agent not listed, so only the agents that run in
# containers (the chief of staff, workers, reviewers) hold a working git key.
set -euo pipefail
PRUNE=0; [ "${1:-}" = --prune ] && { PRUNE=1; shift; }
HERE=$(cd "$(dirname "$0")" && pwd)
source "$HERE/../paths.sh"
A=$HIVE_CONFIG_DIR/agents
for r in "$HIVE_REPOS_DIR"/*.git; do
  install -m 755 "$HERE/pre-receive" "$r/hooks/pre-receive"
done
ACTORS=("$@"); [ ${#ACTORS[@]} -gt 0 ] || ACTORS=($(cd "$A" && ls *.key | sed 's/\.key$//'))
mkdir -p "$HOME/.ssh"; touch "$HOME/.ssh/authorized_keys"; chmod 600 "$HOME/.ssh/authorized_keys"
for a in "${ACTORS[@]}"; do
  [ -f "$A/$a.ssh" ] || ssh-keygen -q -t ed25519 -N '' -C "hive-agent:$a" -f "$A/$a.ssh"
  sed -i "/ hive-agent:$a\$/d" "$HOME/.ssh/authorized_keys"
  echo "command=\"$HERE/hive-git-shell $a\",restrict $(cut -d' ' -f1,2 "$A/$a.ssh.pub") hive-agent:$a" \
    >> "$HOME/.ssh/authorized_keys"
done
if [ "$PRUNE" = 1 ]; then
  for old in $(grep -o ' hive-agent:[^ ]*$' "$HOME/.ssh/authorized_keys" | sed 's/ hive-agent://' | sort -u); do
    if ! printf '%s\n' "${ACTORS[@]}" | grep -qxF "$old"; then
      sed -i "/ hive-agent:$old\$/d" "$HOME/.ssh/authorized_keys"
      rm -f "$A/$old.ssh" "$A/$old.ssh.pub"
      echo "removed git access for $old"
    fi
  done
fi
for f in /etc/ssh/ssh_host_*_key.pub; do echo "127.0.0.1 $(cut -d' ' -f1,2 "$f")"; done > "$A/known_hosts"
echo "hooks in $(ls -d "$HIVE_REPOS_DIR"/*.git | wc -l) repositories; git keys for: ${ACTORS[*]}"
