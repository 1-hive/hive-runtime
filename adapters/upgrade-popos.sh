#!/usr/bin/env bash
# proto-hive's pop-os (proto-hive deploy/popos.md): bring /opt/hive/hive-runtime to REF, refresh the hive files that
# install-agent.py copied into the bots there (ProtoCosmo2's channel and tool, protocosmopopos's hive-task and skill,
# the hive rule), and restart the sidecars and reviewer runners so they run the new code. The bots themselves aren't
# restarted: Iter loads its channel and tool on each call, OpenClaw runs hive-task on each call.
# A reviewer in the middle of a review is left alone (restart it later: systemctl restart hive-reviewer-<user>).
# Run as root; read it from git so the checkout it changes isn't the one running it:
#
#   sudo git -C /opt/hive/hive-runtime fetch -q --tags origin
#   sudo git -C /opt/hive/hive-runtime show v0.5.41:adapters/upgrade-popos.sh | sudo bash -s v0.5.41
set -euo pipefail
REF=${1:?the hive-runtime tag to run, e.g. v0.5.41}
[ "$(id -u)" = 0 ] || { echo "run it with sudo" >&2; exit 2; }
RT=/opt/hive/hive-runtime PY=/opt/hive/venv/bin/python COS=@protohive_cos_bot
IA=$RT/adapters/install-agent.py
ITER=/home/openclaw/research-agent/projects/omegaclaw/protocosmo2/iter-port/repos
RA=/home/openclaw/research-agent
HT=/var/lib/hive-protocosmopopos/hook-token

echo "== hive-runtime: was $(git -C $RT describe --tags 2>/dev/null || echo '?')"
git -C $RT fetch -q --tags origin
git -C $RT checkout -q "$REF"
chmod -R a+rX $RT
echo "   now $(git -C $RT describe --tags)"

echo "== ProtoCosmo2 (Iter)"
if [ -f "$ITER/.hive/config.json" ]; then
  $PY $IA iter --folder "$ITER" --cos-handle $COS
else
  echo "   not on the hive here: skipped"
fi

echo "== protocosmopopos (OpenClaw)"
if [ -d "$RA/skills/hive" ]; then
  hook=(); [ -f $HT ] && hook=(--hook-token-file $HT)
  $PY $IA openclaw --workspace $RA --config /home/openclaw/.openclaw/openclaw.json \
    --queue /var/lib/hive-queues/protocosmopopos --adapter-dir /opt/hive/agent-adapter --home /home/openclaw \
    --cos-handle $COS "${hook[@]}"
  [ -f $HT ] && chown hive-protocosmopopos: $HT
else
  echo "   not on the hive here: skipped"
fi

echo "== sidecars and reviewers"
for u in $(systemctl list-units 'hive-sidecar-*' 'hive-reviewer-*' --state=active --no-legend --plain | awk '{print $1}'); do
  user=$(systemctl show -p User --value "$u")
  if [[ $u == hive-reviewer-* ]] && [ -n "$user" ] && pgrep -u "$user" -f 'codex|claude' >/dev/null; then
    echo "   $u is reviewing: not restarted (later: sudo systemctl restart $u)"
    continue
  fi
  systemctl restart "$u" && echo "   restarted $u"
done
sleep 5
systemctl list-units 'hive-sidecar-*' 'hive-reviewer-*' --all --no-legend --plain | awk '{print "   " $1, $3, $4}'
echo "== done: the Floor shows each pop-os card's adapter (\"hive adapter …\") within a minute"
