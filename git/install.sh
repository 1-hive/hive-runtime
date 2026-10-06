#!/usr/bin/env bash
# Set up agents' git access over SSH (1-hive PLAN, Phase D item 3). Run by the operator:
# - the pre-receive hook in every bare repository under ~/repos;
# - an SSH key per agent (~/.config/hive/agents/<actor>.ssh), restricted in
#   ~/.ssh/authorized_keys to `hive-git-shell <actor>` (no shell, no forwarding);
# - ~/.config/hive/agents/known_hosts: the host's sshd as containers reach it (10.0.2.2).
#   tools/git/install.sh [actor...]     (default: every agent key in ~/.config/hive/agents)
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
A=$HOME/.config/hive/agents
for r in "$HOME"/repos/*.git; do
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
for f in /etc/ssh/ssh_host_*_key.pub; do echo "10.0.2.2 $(cut -d' ' -f1,2 "$f")"; done > "$A/known_hosts"
echo "hooks in $(ls -d "$HOME"/repos/*.git | wc -l) repositories; git keys for: ${ACTORS[*]}"
