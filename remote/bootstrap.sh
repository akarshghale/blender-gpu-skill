#!/bin/bash
# blender-gpu bootstrap: idempotently prepares a fresh Vast.ai instance.
#   bootstrap.sh <version> <sha256> <url> [<url>...]
# Installs a pinned, checksum-verified Blender build into /opt/blender-<version>,
# installs the few shared libraries headless Blender needs, then probes which
# Cycles GPU backends actually work. Writes results under $B/status.
# Exit codes: 0 ok, 10 apt failed, 11 download/checksum failed, 12 extract
# failed, 13 Blender does not start, 14 no usable NVIDIA GPU backend.
set -uo pipefail
B=${BGPU_REMOTE_ROOT:-/workspace/bgpu}
VER=$1; SHA=$2; shift 2
mkdir -p "$B/status" "$B/cache"
log() { echo "[bootstrap $(date -u +%T)] $*"; }

# Test hook: the local mock harness supplies its own Blender.
if [ -n "${BGPU_BLENDER_BIN:-}" ]; then
  BL=$BGPU_BLENDER_BIN
else
  DEST=/opt/blender-$VER
  BL=$DEST/blender

  # 1. System libraries (Blender's Linux build bundles almost everything else).
  PKGS="ca-certificates curl xz-utils libx11-6 libxi6 libxxf86vm1 libxfixes3 libxrender1 libxkbcommon0 libsm6 libice6 libgl1 libegl1"
  missing=""
  for p in $PKGS; do dpkg -s "$p" >/dev/null 2>&1 || missing="$missing $p"; done
  if [ -n "$missing" ]; then
    log "installing:$missing"
    export DEBIAN_FRONTEND=noninteractive
    for i in 1 2 3; do
      apt-get update -qq && apt-get install -y -qq --no-install-recommends $missing >/dev/null && break
      [ $i = 3 ] && { log "apt-get failed"; exit 10; }; sleep 10
    done
  fi

  # 2. Pinned Blender, verified by SHA-256 before use.
  if [ ! -x "$BL" ]; then
    TARBALL=$B/cache/blender-$VER-linux-x64.tar.xz
    ok=0
    if [ -f "$TARBALL" ] && echo "$SHA  $TARBALL" | sha256sum -c --quiet - 2>/dev/null; then ok=1; fi
    for url in "$@"; do
      [ $ok = 1 ] && break
      log "downloading $url"
      if curl -fL --retry 3 --retry-delay 5 --connect-timeout 20 -sS -o "$TARBALL.part" "$url" \
         && echo "$SHA  $TARBALL.part" | sha256sum -c --quiet -; then
        mv "$TARBALL.part" "$TARBALL"; ok=1
      else
        log "download or checksum failed for $url"; rm -f "$TARBALL.part"
      fi
    done
    [ $ok = 1 ] || exit 11
    log "checksum OK, extracting"
    rm -rf "$DEST.tmp" && mkdir -p "$DEST.tmp" \
      && tar -xJf "$TARBALL" -C "$DEST.tmp" --strip-components=1 \
      && mv "$DEST.tmp" "$DEST" || { log "extract failed"; exit 12; }
    [ "${BGPU_KEEP_TARBALL:-0}" = 1 ] || rm -f "$TARBALL"   # free disk after extraction
  fi
fi
echo "$BL" > "$B/status/blender_bin"

# 3. Driver/GPU facts for the job report.
(nvidia-smi --query-gpu=name,memory.total,driver_version,compute_cap --format=csv 2>&1 || echo "nvidia-smi unavailable") > "$B/status/nvidia-smi.txt"
log "$(head -2 "$B/status/nvidia-smi.txt" | tail -1)"

# 4. Ask Cycles which backends really work on this machine.
"$BL" -b --factory-startup --python-exit-code 1 --python-expr '
import bpy, json, os, sys
out = {"blender": bpy.app.version_string, "backends": {}}
prefs = bpy.context.preferences.addons["cycles"].preferences
for kind in ("OPTIX", "CUDA"):
    try:
        prefs.compute_device_type = kind
        prefs.get_devices()
        out["backends"][kind] = [d.name for d in prefs.devices if d.type == kind]
    except Exception as e:
        out["backends"][kind] = []
        out.setdefault("errors", {})[kind] = str(e)
p = os.path.join(os.environ.get("BGPU_REMOTE_ROOT", "/workspace/bgpu"), "status", "gpu_probe.json")
json.dump(out, open(p, "w"))
print("GPU_PROBE", json.dumps(out))
' > "$B/status/probe.log" 2>&1 || { log "Blender failed to start"; tail -20 "$B/status/probe.log"; exit 13; }
grep GPU_PROBE "$B/status/probe.log" | sed 's/^GPU_PROBE /probe: /'
if ! grep -q '"OPTIX": \["\|"CUDA": \["' "$B/status/gpu_probe.json"; then
  [ "${BGPU_ALLOW_CPU:-0}" = 1 ] || { log "no OptiX or CUDA device visible to Blender"; exit 14; }
fi
log "ready"
