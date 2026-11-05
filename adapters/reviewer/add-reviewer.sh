#!/usr/bin/env bash
# Add a reviewer runner modelled on one that already runs on this machine: its own Unix user and record key, the
# same harness, login, git routes and settings, another model. Run as root, in two steps:
#
#   add-reviewer.sh setup <from-user> <new-user> <actor> <model>
#       e.g. add-reviewer.sh setup astra sol reviewer.sol gpt-5.6-sol
#       Makes user <new-user> (home /var/lib/<new-user>), an SSH key and a record key; copies <from-user>'s git URL
#       rules and harness login (~/.codex, ~/.claude, ~/.config/openclaw, whichever exist); writes reviewer.json from
#       <from-user>'s, with this actor, home and the model after --model in review_cmd; writes the unit
#       hive-reviewer-<new-user> from hive-reviewer-<from-user>'s. Prints both public keys for the operators.
#
#   add-reviewer.sh start <from-user> <new-user>
#       After the operators registered the actor and its SSH key: clones the workspace from where <from-user>'s
#       clone fetches, then enables and starts the unit.
#
# The login is a copy: both reviewers then spend the same account. Log the new user in separately to split them.
set -euo pipefail
STEP=${1:-} FROM=${2:-} NEW=${3:-}
[ -n "$STEP" ] && [ -n "$FROM" ] && [ -n "$NEW" ] || { sed -n 2,16p "$0" >&2; exit 2; }
FH=$(getent passwd "$FROM" | cut -d: -f6); [ -n "$FH" ] || { echo "no user $FROM" >&2; exit 2; }
NH=/var/lib/$NEW
as_new() { sudo -u "$NEW" -H "$@"; }

case $STEP in
setup)
  ACTOR=${4:?actor, e.g. reviewer.sol} MODEL=${5:?model, e.g. gpt-5.6-sol}
  [ -f "$FH/reviewer.json" ] || { echo "$FH/reviewer.json not found" >&2; exit 2; }
  [ -f "/etc/systemd/system/hive-reviewer-$FROM.service" ] || { echo "no unit hive-reviewer-$FROM" >&2; exit 2; }
  id "$NEW" >/dev/null 2>&1 || useradd -r -m -d "$NH" -s /bin/bash "$NEW"
  [ -f "$NH/.ssh/id_ed25519" ] || as_new sh -c 'mkdir -p -m 700 ~/.ssh && ssh-keygen -q -t ed25519 -N "" -f ~/.ssh/id_ed25519'
  [ -f "$NH/record.key" ] || { as_new /opt/hive/bin/hive keygen --out "$NH/record.key" >/dev/null; chmod 600 "$NH/record.key"; }
  as_new git config --global --replace-all safe.directory '*'
  sudo -u "$FROM" -H git config --global --get-regexp '^url\..*\.insteadof$' 2>/dev/null | while read -r k v; do
    as_new git config --global "$k" "$v"
  done
  for d in .codex .claude .config/openclaw; do
    if [ -e "$FH/$d" ]; then mkdir -p "$(dirname "$NH/$d")"; rm -rf "${NH:?}/$d"; cp -a "$FH/$d" "$NH/$d"; fi
  done
  chown -R "$NEW:$NEW" "$NH"
  /opt/hive/venv/bin/python - "$FH" "$NH" "$ACTOR" "$MODEL" <<'PY'
import json, sys
fh, nh, actor, model = sys.argv[1:]
c = json.loads(open(f"{fh}/reviewer.json").read().replace(fh, nh))
c["actor"] = actor
c["key_file"] = f"{nh}/record.key" if "key_file" in c else c.get("key_file")
cmd = c["review_cmd"]
if "--model" in cmd:
    cmd[cmd.index("--model") + 1] = model
else:
    sys.exit("review_cmd has no --model: set the model in the new reviewer.json by hand")
json.dump(c, open(f"{nh}/reviewer.json", "w"), indent=1)
PY
  chown "$NEW:$NEW" "$NH/reviewer.json"; chmod 600 "$NH/reviewer.json"
  sed -e "s#$FH#$NH#g" -e "s/^User=$FROM\$/User=$NEW/" -e "s/^Group=$FROM\$/Group=$NEW/" \
      "/etc/systemd/system/hive-reviewer-$FROM.service" > "/etc/systemd/system/hive-reviewer-$NEW.service"
  systemctl daemon-reload
  echo "== $NH/reviewer.json (check actor, paths, model)"
  /opt/hive/venv/bin/python -c 'import json,sys; c=json.load(open(sys.argv[1])); print(json.dumps({k: c[k] for k in ("actor","key_file","review_cmd") if k in c}, indent=1))' "$NH/reviewer.json"
  echo "== /etc/systemd/system/hive-reviewer-$NEW.service"
  cat "/etc/systemd/system/hive-reviewer-$NEW.service"
  echo "== send these two lines to the operators (public keys only)"
  echo "record key for $ACTOR: $(/opt/hive/venv/bin/python -c 'import sys; from hiverecord.keys import SigningKey; print(SigningKey.from_file(sys.argv[1]).public)' "$NH/record.key")"
  echo "git-keys line: $ACTOR $(cat "$NH/.ssh/id_ed25519.pub")"
  ;;
start)
  URL=$(sudo -u "$FROM" -H git -C "$FH/workspace" remote get-url origin)
  WS=$(/opt/hive/venv/bin/python -c 'import json,sys; c=json.load(open(sys.argv[1])); w=c.get("workspace"); print(w.get("clone") if isinstance(w, dict) else w or "")' "$NH/reviewer.json")
  WS=${WS:-$NH/workspace}
  [ -d "$WS/.git" ] || as_new env GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new" git clone -q "$URL" "$WS"
  systemctl enable --now "hive-reviewer-$NEW"
  sleep 5; systemctl status --no-pager "hive-reviewer-$NEW" | head -6
  ;;
*) sed -n 2,16p "$0" >&2; exit 2;;
esac
