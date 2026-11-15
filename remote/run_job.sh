#!/bin/bash
# blender-gpu job runner. Started detached (setsid nohup) by the controller so
# a dropped SSH connection never kills a render. The controller polls
# $J/state and $J/exit_code.
#   run_job.sh <budget_seconds>
B=${BGPU_REMOTE_ROOT:-/workspace/bgpu}
J=$B/job
BUDGET=${1:-6000}
mkdir -p "$J/output" "$J/logs"
echo running > "$J/state"
rm -f "$J/exit_code" "$J/MANIFEST.sha256"

BL=$(cat "$B/status/blender_bin")
REL=$(cat "$J/blend_rel" 2>/dev/null)
ARGS=(-b --factory-startup)
[ -f "$J/autoexec" ] && ARGS+=(-y)
# Script-only jobs may have no .blend: Blender then starts with its default scene.
[ -n "$REL" ] && ARGS+=("$J/input/$REL")
ARGS+=(--python-exit-code 7 --python "$B/render.py" -- "$J/spec.json")

TO=$(command -v timeout || true)
if [ -n "$TO" ]; then
  "$TO" --kill-after=30 "$BUDGET" "$BL" "${ARGS[@]}" >"$J/logs/blender.log" 2>&1 &
else
  "$BL" "${ARGS[@]}" >"$J/logs/blender.log" 2>&1 &
fi
# The watchdog and the controller stop a render by signalling this PID;
# this script then still writes the manifest for whatever finished.
echo $! >"$B/status/render.pid"
wait $!
rc=$?
[ $rc = 124 ] && echo "[bgpu] job budget of ${BUDGET}s exceeded; render stopped" >>"$J/logs/blender.log"

# Checksum manifest of everything produced, used by the controller to verify
# the download before the instance is destroyed.
if command -v sha256sum >/dev/null; then SUM="sha256sum"; else SUM="shasum -a 256"; fi
(cd "$J/output" && find . -type f ! -name '.*.partial*' | LC_ALL=C sort | while IFS= read -r f; do $SUM "$f"; done) \
  >"$J/MANIFEST.sha256.tmp" && mv "$J/MANIFEST.sha256.tmp" "$J/MANIFEST.sha256"
echo $rc >"$J/exit_code"
echo done >"$J/state"
