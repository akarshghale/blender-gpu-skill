#!/bin/bash
# One-time, free setup of blender-gpu on this Mac. Safe to re-run.
# It never rents anything. Account steps (API key, SSH key) stay manual:
# see "Setup" in SKILL.md.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
BIN=${BGPU_BIN_DIR:-$HOME/.local/bin}

mkdir -p "$BIN" "$HOME/BlenderGPU"
chmod +x "$HERE/blender-gpu" "$HERE/blender-gpu-panic"
ln -sfn "$HERE/blender-gpu" "$BIN/blender-gpu"
ln -sfn "$HERE/blender-gpu-panic" "$BIN/blender-gpu-panic"
echo "linked blender-gpu and blender-gpu-panic into $BIN"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "NOTE: add $BIN to your PATH";; esac

python3 "$HERE/blender-gpu" install-reaper
echo
python3 "$HERE/blender-gpu" doctor
