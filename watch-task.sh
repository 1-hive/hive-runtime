#!/usr/bin/env bash
# Wait until a task needs the chief of staff, then print why and exit.
# Minimal stand-in for the Phase E supervisor's detection side.
#
#   watch-task.sh <task-id> [claude-session-id | pid:<pid>] [max-hours]
#
# Exits on: task.blocked, task.result_posted, task.released, review.recorded,
# a gateway refusal of anyone on this task, the owner silent for longer than
# its lease's check-in interval (+10 min grace, counted from no earlier than
# the watch's start, so a restarted worker gets a full interval), or the
# session disappearing.
set -uo pipefail
TASK=$1 SESSION=${2:-} MAX_H=${3:-12}
export HIVE_URL=http://127.0.0.1:8470 HIVE_ID=1-hive HIVE_KEY_FILE=$HOME/.config/hive/agents/cos.key
START=$(hive health | python3 -c 'import json,sys; print(json.load(sys.stdin)["head"])')
WATCH_START=$(date +%s)
END=$(( WATCH_START + MAX_H * 3600 ))
while [ "$(date +%s)" -lt "$END" ]; do
  why=$(hive events --after "$START" 2>/dev/null | python3 -c '
import json, sys
task = sys.argv[1]
for l in sys.stdin:
    l = l.strip()
    if not l: continue
    e = json.loads(l); d = e.get("data") or {}
    if e.get("task") == task and e["type"] in ("task.blocked", "task.result_posted", "task.released", "review.recorded"):
        print(e["position"], e["type"], json.dumps(d)[:200]); break
    if e["type"] == "gateway.rejected" and (e.get("task") == task or (d.get("refused") or {}).get("task") == task):
        print(e["position"], "REFUSED", d.get("code"), d.get("reason", "")[:150]); break
' "$TASK")
  [ -n "$why" ] && { echo "EVENT: $why"; exit 0; }
  stale=$(hive task "$TASK" 2>/dev/null | python3 -c '
import json, sys, datetime
t = json.load(sys.stdin); t = t.get("task", t)
lease = (t.get("ext") or {}).get("lease") or {}
every = lease.get("checkin_every_seconds")
last = (t.get("last_activity") or {}).get("at") or t.get("assigned_at")
if every and last and t.get("status") in ("assigned", "in_progress"):
    last_t = max(datetime.datetime.fromisoformat(last.replace("Z", "+00:00")).timestamp(), float(sys.argv[2]))
    age = datetime.datetime.now(datetime.timezone.utc).timestamp() - last_t
    if age > every + 600: print(f"owner silent {int(age/60)} min (check-in every {every//60} min)")
' "$WATCH_START")
  [ -n "$stale" ] && { echo "STALE: $stale"; exit 0; }
  if [ "${SESSION#pid:}" != "$SESSION" ]; then
    kill -0 "${SESSION#pid:}" 2>/dev/null || { echo "PROCESS GONE: $SESSION"; exit 0; }
  elif [ -n "$SESSION" ]; then
    st=$(claude agents --json 2>/dev/null | python3 -c '
import json, sys
d = json.load(sys.stdin); d = d if isinstance(d, list) else d.get("agents", d)
print(next((str(a.get("status")) + "/" + str(a.get("state")) for a in d if a.get("id") == sys.argv[1]), "gone"))' "$SESSION")
    [ "$st" = gone ] && { echo "SESSION GONE: $SESSION"; exit 0; }
  fi
  sleep 60
done
echo "TIMEOUT: nothing after $MAX_H h"
