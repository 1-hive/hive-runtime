#!/usr/bin/env bash
# Keep this host's copy of the hive's registry current, for a host that runs sidecars or reviewers but not the
# record (e.g. a second machine reaching the record over a tailnet). Run as root, from a timer.
#
#   registry-pull.sh --as USER --from URL --to FILE [--restart UNIT]...
#   registry-pull.sh --install --as USER --from URL --to FILE [--restart UNIT]...   (a systemd timer, every 5 min)
#
# USER is a local user that can already fetch from the hive's repositories (a sidecar's or reviewer's user,
# with its SSH key and the hive's git route); URL is the registry repository new-repo.py --registry-repo keeps
# (e.g. file:///var/lib/hive/repos/hive-registry.git, which USER's insteadOf rule turns into the SSH route).
# Fetches its main as USER into USER's ~/.cache/hive-registry.git, and when registry.json there differs from FILE
# and is valid JSON with a "repositories" object, replaces FILE (mode 644, atomically) and restarts each UNIT
# (only units that need it: hive-runtime >= v0.5.28 sidecars and reviewers reread the file themselves).
# Prints one line when it changed something, nothing otherwise.
set -euo pipefail
AS= FROM= TO= RESTART=() INSTALL=
ARGS=("$@")
while [ $# -gt 0 ]; do
  case "$1" in
    --as) AS=$2; shift 2 ;;
    --from) FROM=$2; shift 2 ;;
    --to) TO=$2; shift 2 ;;
    --restart) RESTART+=("$2"); shift 2 ;;
    --install) INSTALL=1; shift ;;
    *) echo "registry-pull: unknown argument $1" >&2; exit 2 ;;
  esac
done
[ -n "$AS" ] && [ -n "$FROM" ] && [ -n "$TO" ] || { sed -n '2,13p' "$0" >&2; exit 2; }
[ "$(id -u)" = 0 ] || { echo "registry-pull: run as root" >&2; exit 2; }
if [ -n "$INSTALL" ]; then
  SELF=$(readlink -f "$0") RUN=()
  for x in "${ARGS[@]}"; do [ "$x" = --install ] || RUN+=("$x"); done
  cat > /etc/systemd/system/hive-registry-pull.service <<UNIT
[Unit]
Description=Keep $TO current from the hive's registry repository (hive-runtime registry-pull.sh)
After=network-online.target

[Service]
Type=oneshot
ExecStart=$SELF ${RUN[*]}
UNIT
  cat > /etc/systemd/system/hive-registry-pull.timer <<UNIT
[Unit]
Description=Pull the hive's registry every 5 minutes

[Timer]
OnBootSec=1min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
UNIT
  systemctl daemon-reload
  systemctl enable -q --now hive-registry-pull.timer
  systemctl start hive-registry-pull.service && echo "hive-registry-pull.timer installed; first pull: OK" \
    || { echo "first pull failed: journalctl -u hive-registry-pull.service" >&2; exit 1; }
  exit 0
fi
HOME_AS=$(getent passwd "$AS" | cut -d: -f6)
[ -n "$HOME_AS" ] || { echo "registry-pull: no user $AS" >&2; exit 2; }
B=$HOME_AS/.cache/hive-registry.git
as() { runuser -u "$AS" -- env HOME="$HOME_AS" GIT_TERMINAL_PROMPT=0 "$@"; }
[ -f "$B/HEAD" ] || as git init -q --bare "$B"
as git -C "$B" fetch -q --no-tags "$FROM" +refs/heads/main:refs/heads/main
TMP=$(mktemp "$(dirname "$TO")/.registry.XXXXXX")
trap 'rm -f "$TMP"' EXIT
as git -C "$B" show main:registry.json > "$TMP"
python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); assert isinstance(r["repositories"], dict)' "$TMP" \
  || { echo "registry-pull: registry.json on $FROM's main isn't a registry; $TO left as it was" >&2; exit 1; }
if cmp -s "$TMP" "$TO"; then exit 0; fi
chmod 644 "$TMP"
mv -f "$TMP" "$TO"
trap - EXIT
for u in "${RESTART[@]}"; do systemctl try-restart "$u" || echo "registry-pull: restarting $u failed" >&2; done
echo "registry-pull: $TO updated from $FROM ($(as git -C "$B" log -1 --format='%h %s' main))${RESTART:+; restarted ${RESTART[*]}}"
