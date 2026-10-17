#!/bin/bash
# blender-gpu onstart: runs on the rented instance at container start, before
# anything else. Its only job is to arm the hard runtime limit, which works
# even if the Mac or the controlling process goes away.
# The controller fills in two placeholders: the DEADLINE (absolute epoch:
# instance creation + limit) and the PROVIDER_HOOKS (bash functions that let
# this instance destroy itself; see providers/base.py).
B=${BGPU_REMOTE_ROOT:-/workspace/bgpu}
mkdir -p "$B/status"
# Never move an existing deadline (the container may restart).
[ -s "$B/deadline" ] || echo __DEADLINE__ > "$B/deadline"
cat > "$B/watchdog.sh" <<'WATCHDOG'
#!/bin/bash
B=${BGPU_REMOTE_ROOT:-/workspace/bgpu}
# --- provider hooks: bgpu_self_destroy, bgpu_self_stop
__PROVIDER_HOOKS__
# --- end provider hooks
echo "$(date -u +%FT%TZ) watchdog armed: instance=${BGPU_INSTANCE_ID:-?} deadline=$(cat "$B/deadline")"
while :; do
  now=$(date +%s); dl=$(cat "$B/deadline" 2>/dev/null || echo 0)
  [ "$now" -ge "$dl" ] && break
  left=$((dl - now)); sleep $((left < 30 ? left : 30))
done
echo "$(date -u +%FT%TZ) runtime limit reached; stopping work and destroying instance ${BGPU_INSTANCE_ID:-?}"
echo ttl_expired > "$B/status/watchdog"
# run_job.sh records the render's PID; stop it before destroying.
if [ -s "$B/status/render.pid" ]; then
  pid=$(cat "$B/status/render.pid"); kill -TERM "$pid" 2>/dev/null; sleep 5; kill -KILL "$pid" 2>/dev/null
fi
for i in 1 2 3 4 5 6 7 8 9 10; do
  if bgpu_self_destroy; then echo; echo "$(date -u +%FT%TZ) destroy accepted"; exit 0; fi
  echo "$(date -u +%FT%TZ) destroy attempt $i failed"; sleep 30
done
# Destroy kept failing: at least stop the instance (ends GPU billing).
for i in 1 2 3 4 5; do bgpu_self_stop && { echo; echo "stop accepted"; exit 0; }; sleep 30; done
echo "$(date -u +%FT%TZ) API unreachable; killing container init as last resort"
[ -d /proc/1 ] && kill -TERM 1 2>/dev/null
WATCHDOG
chmod +x "$B/watchdog.sh"
if ! pgrep -f "$B/watchdog.sh" >/dev/null 2>&1; then
  SETSID=$(command -v setsid || true)
  nohup $SETSID bash "$B/watchdog.sh" >>"$B/watchdog.log" 2>&1 </dev/null &
fi
