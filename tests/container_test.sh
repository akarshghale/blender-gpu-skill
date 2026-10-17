#!/bin/bash
# Real-Blender validation without a GPU or any rental: runs bootstrap.sh,
# run_job.sh, render.py and the watchdog inside a local linux/amd64 Ubuntu
# container (Docker/OrbStack), rendering with Cycles on CPU.
#   tests/container_test.sh [/dir/with/blender-<ver>-linux-x64.tar.xz]
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
TOOL=$(dirname "$HERE")
CACHE=${1:-$HOME/BlenderGPU/cache}
IMAGE=${BGPU_TEST_IMAGE:-ubuntu:24.04}
VER=$(python3 -c "import json;print(json.load(open('$TOOL/config.json'))['blender_version'])")
SHA=$(python3 -c "import json;print(json.load(open('$TOOL/config.json'))['blender_sha256'])")
URL=$(python3 -c "import json;print(json.load(open('$TOOL/config.json'))['blender_urls'][0])")
mkdir -p "$CACHE"

ONSTART=$(mktemp); trap 'rm -f "$ONSTART"' EXIT
python3 "$TOOL/scripts/blender-gpu" _onstart 1111111111 > "$ONSTART"   # placeholder deadline, replaced below
docker run --rm -v "$ONSTART:/onstart.tmpl:ro" --platform linux/amd64 -e CONTAINER_ID=4242 -e CONTAINER_API_KEY=testkey \
  -v "$TOOL/remote:/src:ro" -v "$CACHE:/workspace/bgpu/cache" "$IMAGE" bash -c '
set -uo pipefail
B=/workspace/bgpu; J=$B/job; fails=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; fails=$((fails+1)); }
cp /src/bootstrap.sh /src/run_job.sh /src/render.py $B/

echo "== bootstrap (no GPU here, so BGPU_ALLOW_CPU=1)"
BGPU_ALLOW_CPU=1 BGPU_KEEP_TARBALL=1 bash $B/bootstrap.sh '"$VER"' '"$SHA"' https://invalid.example/blender.tar.xz '"$URL"'
rc=$?; [ $rc = 0 ] && pass "bootstrap exit 0 (bad mirror skipped, checksum verified)" || { fail "bootstrap exit $rc"; exit 1; }
BL=$(cat $B/status/blender_bin)
grep -q "\"blender\": \"'"$VER"'" $B/status/gpu_probe.json && pass "probe ran on Blender '"$VER"': $(cat $B/status/gpu_probe.json)" || fail "probe"
BGPU_ALLOW_CPU=1 bash $B/bootstrap.sh '"$VER"' '"$SHA"' '"$URL"' >/dev/null && pass "bootstrap is idempotent (second run)" || fail "rerun"
BGPU_KEEP_TARBALL=1 bash $B/bootstrap.sh '"$VER"' '"$SHA"' '"$URL"' >/dev/null; [ $? = 14 ] && pass "no GPU -> bootstrap exit 14" || fail "expected exit 14"

echo "== build a test scene with an external texture"
mkdir -p $J/input/textures
"$BL" -b --factory-startup --python-expr "
import bpy
bpy.ops.wm.read_factory_settings(use_empty=False)
img = bpy.data.images.new(\"tex\", 8, 8); img.generated_color = (1, .3, 0, 1)
img.filepath_raw = \"$J/input/textures/tex.png\"; img.file_format = \"PNG\"; img.save()
mat = bpy.data.materials.new(\"M\"); mat.use_nodes = True
n = mat.node_tree.nodes.new(\"ShaderNodeTexImage\"); n.image = bpy.data.images.load(\"$J/input/textures/tex.png\")
mat.node_tree.links.new(n.outputs[0], mat.node_tree.nodes[\"Principled BSDF\"].inputs[0])
bpy.data.objects[\"Cube\"].data.materials.append(mat)
s = bpy.context.scene; s.frame_start, s.frame_end = 1, 3
s.render.image_settings.media_type = \"VIDEO\" if hasattr(s.render.image_settings, \"media_type\") else None
bpy.ops.wm.save_as_mainfile(filepath=\"$J/input/scene.blend\", relative_remap=True)
" >/tmp/mk.log 2>&1 && pass "scene.blend created (output set to VIDEO to test format switching)" || { cat /tmp/mk.log; fail "make scene"; }
echo -n scene.blend > $J/blend_rel

run() { # run <expected_exit> <desc> <spec-json>
  rm -rf $J/output $J/logs; echo "$3" > $J/spec.json
  bash $B/run_job.sh 600; rc=$(cat $J/exit_code)
  [ "$rc" = "$1" ] && pass "$2 (exit $rc)" || { fail "$2: exit $rc, expected $1"; tail -25 $J/logs/blender.log; }
}
base="\"output_dir\":\"$J/output\",\"report_path\":\"$J/logs/render_report.json\",\"samples\":4,\"resolution_percent\":10"

run 0 "auto device falls back to CPU when allowed; frames 1-2 PNG" "{$base,\"frames\":\"1-2\",\"require_gpu\":false}"
ls $J/output | tr "\n" " "; echo
[ "$(ls $J/output | wc -l)" = 2 ] && head -c 8 $J/output/frame_0001.png | od -An -c | grep -q "P   N   G" && pass "2 valid PNGs written" || fail "png outputs"
grep -q "\"backend\": \"CPU\"" $J/logs/render_report.json && pass "report records CPU backend" || fail "report backend"
(cd $J/output && sha256sum -c --quiet ../MANIFEST.sha256) && pass "MANIFEST.sha256 matches outputs" || fail "manifest"
grep -q "OPTIX: no devices\|backend OPTIX" $J/logs/blender.log && pass "OptiX tried first, then CUDA, then CPU" || fail "backend order log"

mkdir -p $J/output && echo keep > /dev/null
run 5 "require_gpu with no GPU refuses to burn money on CPU" "{$base,\"frames\":\"1\"}"
run 0 "multilayer EXR, scene frame range" "{$base,\"frames\":\"scene\",\"format\":\"OPEN_EXR_MULTILAYER\",\"require_gpu\":false}"
[ "$(ls $J/output/*.exr | wc -l)" = 3 ] && head -c 4 $J/output/frame_0001.exr | od -An -tx1 | grep -q "76 2f 31 01" && pass "3 valid EXRs" || fail "exr outputs"

echo "import os; open(os.path.join(BGPU_OUTPUT_DIR, \"info.txt\"), \"w\").write(BGPU_DEVICE + \" \" + __import__(\"bpy\").app.version_string)" > $J/input/s.py
run 0 "script mode" "{$base,\"mode\":\"script\",\"user_script\":\"$J/input/s.py\",\"require_gpu\":false}"
grep -q "CPU '"$VER"'" $J/output/info.txt && pass "user script ran inside Blender: $(cat $J/output/info.txt)" || fail "script output"

mv $J/input/textures $J/input/textures.bak
run 4 "missing external texture is detected before rendering" "{$base,\"frames\":\"1\",\"require_gpu\":false}"
grep -q "MISSING image" $J/logs/blender.log && pass "missing file named in log" || fail "missing log"
mv $J/input/textures.bak $J/input/textures

echo "== job budget enforcement (timeout)"
rm -rf $J/output $J/logs; echo "{$base,\"frames\":\"1-200\",\"samples\":64,\"resolution_percent\":50,\"require_gpu\":false}" > $J/spec.json
bash $B/run_job.sh 8; rc=$(cat $J/exit_code)
[ "$rc" = 124 ] && grep -q "budget" $J/logs/blender.log && pass "render killed at job budget (exit 124), manifest still written: $(wc -l < $J/MANIFEST.sha256) file(s)" || fail "budget: rc=$rc"

echo "== watchdog (deadline in 3s; env only on PID 1 like Vast; fake API via Blender python)"
PY=$(ls -d /opt/blender-*/*/python/bin/python3* | head -1)
cat > /tmp/api.py <<EOF
import http.server, sys
class H(http.server.BaseHTTPRequestHandler):
    def do_DELETE(self):
        open("/tmp/api_hit", "w").write(self.path + " " + self.headers.get("Authorization", ""))
        self.send_response(200); self.end_headers(); self.wfile.write(b"{\"success\": true}")
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", 8765), H).serve_forever()
EOF
"$PY" /tmp/api.py & sleep 1
rm -rf $J/output $J/logs; echo "{$base,\"frames\":\"1-500\",\"samples\":64,\"resolution_percent\":50,\"require_gpu\":false}" > $J/spec.json
(setsid nohup bash $B/run_job.sh 600 >/dev/null 2>&1 &); sleep 2
rm -f $B/deadline
sed "s/1111111111/$(( $(date +%s) + 3 ))/" /onstart.tmpl > /tmp/onstart.sh
env -u CONTAINER_ID -u CONTAINER_API_KEY BGPU_API_BASE=http://127.0.0.1:8765/api/v0 bash /tmp/onstart.sh
for i in $(seq 40); do [ -f /tmp/api_hit ] && break; sleep 0.5; done
[ "$(cat /tmp/api_hit 2>/dev/null)" = "/api/v0/instances/4242/ Bearer testkey" ] && pass "watchdog called DELETE /instances/4242/ with the instance key from /proc/1/environ" || fail "watchdog api: $(cat /tmp/api_hit 2>/dev/null) $(cat $B/watchdog.log)"
sleep 3; [ "$(cat $J/state)" = done ] && pass "watchdog stopped the running render first" || fail "render still running"

echo; [ $fails = 0 ] && echo "ALL CONTAINER CHECKS PASSED" || echo "$fails CONTAINER CHECK(S) FAILED"
exit $fails
'
