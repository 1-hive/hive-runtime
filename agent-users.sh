#!/usr/bin/env bash
# Create one OS user per agent actor (SPEC §6.6: no two agents share an OS user), so an agent
# that escapes its container is an unprivileged user, not the operator. Run once, as root:
#
#   sudo deploy/agent-users.sh [actor...]     (default: the agent actors below)
#
# Each actor <a> (e.g. worker.claude.1) gets:
# - user hive-<a with dots as dashes>, home /var/lib/1hive-agents/<user>, no login shell,
#   in group `hive`, with its own sub-id range for rootless Podman, and lingering (so its
#   Podman runs without a login session);
# - nothing else: its keys reach its containers as Podman secrets, and it may enter only the
#   task folders the launcher grants it (ACLs), so it can't read the operator's files.
# The operator's user may run /usr/local/bin/hive-agent-podman as these users (sudoers), which
# is how tools/launch-task.sh starts their containers.
# Re-running is safe: existing users, ranges and files are kept.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
OPERATOR=${SUDO_USER:-omegahive}
ACTORS=("$@"); [ ${#ACTORS[@]} -gt 0 ] || ACTORS=(worker.claude.1 reviewer.claude.1 reviewer.codex.1)
BASE=/var/lib/1hive-agents

groupadd -f hive
usermod -aG hive "$OPERATOR"
mkdir -p "$BASE"; chmod 755 "$BASE"

# Sub-id ranges: after the highest range in use, 65536 each.
next_range() { awk -F: '{e=$2+$3; if (e>m) m=e} END {print (m>165536 ? m : 165536)}' /etc/subuid /etc/subgid; }

USERS=()
for a in "${ACTORS[@]}"; do
  [[ "$a" =~ ^[a-z0-9][a-z0-9._-]{0,40}$ ]] || { echo "bad actor $a" >&2; exit 1; }
  u=hive-${a//./-}
  USERS+=("$u")
  if ! id "$u" >/dev/null 2>&1; then
    useradd --create-home --home-dir "$BASE/$u" --shell /usr/sbin/nologin --groups hive \
      --comment "1-hive agent $a" "$u"
  fi
  if ! grep -q "^$u:" /etc/subuid; then
    r=$(next_range)
    usermod --add-subuids "$r-$((r + 65535))" --add-subgids "$r-$((r + 65535))" "$u"
  fi
  chmod 700 "$BASE/$u"
  loginctl enable-linger "$u"
  echo "$u ($a): uid $(id -u "$u"), subuid $(grep "^$u:" /etc/subuid | cut -d: -f2,3)"
done

# The operator's home and the task root: traversal only, so a granted task folder is reachable.
for d in "/home/$OPERATOR" "/home/$OPERATOR/work" "/home/$OPERATOR/work/1hive"; do
  setfacl -m g:hive:x "$d"
done

# Podman as an agent user, with its own runtime directory (sudo doesn't set one).
cat > /usr/local/bin/hive-agent-podman <<'EOF'
#!/bin/sh
# Run as an agent user (via sudo): its own rootless Podman. 1-hive deploy/agent-users.sh.
XDG_RUNTIME_DIR=/run/user/$(id -u); export XDG_RUNTIME_DIR
HOME=$(getent passwd "$(id -un)" | cut -d: -f6); export HOME
cd "$HOME" || exit 1
exec /usr/bin/podman "$@"
EOF
chmod 755 /usr/local/bin/hive-agent-podman

SUDOERS=/etc/sudoers.d/1hive-agents
{ echo "# 1-hive: the operator starts agent containers as the agents' own users (deploy/agent-users.sh)."
  echo "Defaults!/usr/local/bin/hive-agent-podman !requiretty"
  # Every agent user there is, not only this run's (a later run may add one actor).
  ALL=$(getent group hive | cut -d: -f4 | tr , '\n' | grep '^hive-' | sort -u | paste -sd,)
  echo "$OPERATOR ALL=($ALL) NOPASSWD: /usr/local/bin/hive-agent-podman"
} > "$SUDOERS.tmp"
visudo -cf "$SUDOERS.tmp" >/dev/null && install -m 440 "$SUDOERS.tmp" "$SUDOERS"; rm -f "$SUDOERS.tmp"
echo "done. Log out and back in (or 'newgrp hive') for $OPERATOR's new group to apply."
