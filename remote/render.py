"""blender-gpu render driver. Runs inside Blender:

    blender -b <file.blend> --factory-startup --python render.py -- <spec.json>

Selects a GPU backend (OptiX first, then CUDA), checks for missing external
files, renders the requested frames one at a time (each written atomically,
already-finished frames are skipped), optionally runs a user Python script,
and writes a JSON report. Exit codes: 0 ok, 3 some frames failed, 4 missing
external files, 5 no GPU backend, 6 user script raised, 7 bad spec.
"""
import json
import os
import re
import sys
import time
import traceback

import bpy

EXT = {"PNG": ".png", "JPEG": ".jpg", "OPEN_EXR": ".exr", "TIFF": ".tif",
       "OPEN_EXR_MULTILAYER": ".exr", "WEBP": ".webp", "BMP": ".bmp"}


def log(msg):
    print("[bgpu] " + msg, flush=True)


def parse_frames(text, scene):
    """'1-10,15,20-24' -> sorted unique ints. None -> current frame."""
    if not text:
        return [scene.frame_current]
    if text == "scene":
        return list(range(scene.frame_start, scene.frame_end + 1, scene.frame_step))
    out = set()
    for part in str(text).split(","):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(-?\d+)\s*-\s*(-?\d+)$", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if b < a:
                raise ValueError("bad frame range: %r" % part)
            out.update(range(a, b + 1))
        else:
            out.add(int(part))
    if not out:
        raise ValueError("empty frame list: %r" % text)
    return sorted(out)


def configure_device(scene, want, require_gpu):
    scene.render.engine = "CYCLES"
    if want == "CPU":
        scene.cycles.device = "CPU"
        return "CPU", []
    prefs = bpy.context.preferences.addons["cycles"].preferences
    order = ["OPTIX", "CUDA"] if want == "auto" else [want]
    for kind in order:
        try:
            prefs.compute_device_type = kind
            prefs.get_devices()
        except Exception as e:  # backend not compiled in / driver too old
            log("backend %s unavailable: %s" % (kind, e))
            continue
        gpus = [d for d in prefs.devices if d.type == kind]
        if not gpus:
            log("backend %s: no devices" % kind)
            continue
        for d in prefs.devices:
            d.use = d.type == kind  # GPU only; CPU+GPU hybrid is usually slower
        scene.cycles.device = "GPU"
        return kind, [d.name for d in gpus]
    if require_gpu:
        log("no usable GPU backend (tried %s) and require_gpu is set" % ", ".join(order))
        sys.exit(5)
    log("WARNING: falling back to CPU rendering")
    scene.cycles.device = "CPU"
    return "CPU", []


def missing_files():
    missing = []

    def check(kind, block, path):
        if not path or path.startswith("<"):
            return
        p = bpy.path.abspath(path, library=getattr(block, "library", None))
        if "<UDIM>" in p or "<UVTILE>" in p:
            return
        if not os.path.exists(p):
            missing.append({"type": kind, "name": block.name, "path": p})

    for img in bpy.data.images:
        if img.source in {"FILE", "SEQUENCE", "MOVIE"} and not img.packed_file:
            check("image", img, img.filepath)
    for lib in bpy.data.libraries:
        if not lib.packed_file:
            check("library", lib, lib.filepath)
    for coll in ("volumes", "cache_files", "movieclips", "sounds", "fonts"):
        for block in getattr(bpy.data, coll, []):
            if not getattr(block, "packed_file", None):
                check(coll, block, getattr(block, "filepath", ""))
    return missing


def set_format(scene, fmt):
    s = scene.render.image_settings
    if hasattr(s, "media_type"):  # Blender 5.x splits image/multilayer/video
        s.media_type = "MULTI_LAYER_IMAGE" if fmt == "OPEN_EXR_MULTILAYER" else "IMAGE"
    s.file_format = fmt


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if not argv:
        log("usage: ... --python render.py -- spec.json")
        sys.exit(7)
    spec = json.load(open(argv[0]))
    out_dir = spec["output_dir"]
    os.makedirs(out_dir, exist_ok=True)
    report = {"blender": bpy.app.version_string, "spec": spec, "frames": [],
              "started": time.time()}
    report_path = spec.get("report_path") or os.path.join(out_dir, "..", "render_report.json")

    def save_report(code):
        report["exit_code"] = code
        report["finished"] = time.time()
        with open(report_path + ".tmp", "w") as f:
            json.dump(report, f, indent=1)
        os.replace(report_path + ".tmp", report_path)

    scene = bpy.data.scenes[spec["scene"]] if spec.get("scene") else bpy.context.scene
    if bpy.context.window is not None:
        bpy.context.window.scene = scene
    if spec.get("camera"):
        scene.camera = bpy.data.objects[spec["camera"]]

    if spec.get("fail_on_missing", True):
        miss = missing_files()
        report["missing_files"] = miss
        if miss:
            for m in miss:
                log("MISSING %(type)s %(name)s: %(path)s" % m)
            log("external files are missing; pack them (File > External Data > Pack Resources) "
                "or upload the project directory. Set fail_on_missing=false to render anyway.")
            save_report(4)
            sys.exit(4)

    if spec.get("engine", "CYCLES") == "CYCLES":
        kind, names = configure_device(scene, spec.get("device", "auto"), spec.get("require_gpu", True))
    else:
        kind, names = scene.render.engine, []
    report["device"] = {"backend": kind, "devices": names}
    log("render device: %s %s" % (kind, names))

    if spec.get("samples"):
        scene.cycles.samples = int(spec["samples"])
    if spec.get("resolution"):
        scene.render.resolution_x, scene.render.resolution_y = spec["resolution"]
    if spec.get("resolution_percent"):
        scene.render.resolution_percentage = int(spec["resolution_percent"])
    fmt = spec.get("format", "PNG")
    set_format(scene, fmt)
    ext = EXT.get(fmt, "." + fmt.lower())

    mode = spec.get("mode", "render")
    if spec.get("user_script"):
        os.environ["BGPU_OUTPUT_DIR"] = out_dir
        log("running user script %s" % spec["user_script"])
        try:
            code = compile(open(spec["user_script"]).read(), spec["user_script"], "exec")
            exec(code, {"__name__": "__main__", "__file__": spec["user_script"],
                        "BGPU_OUTPUT_DIR": out_dir, "BGPU_SPEC": spec, "BGPU_DEVICE": kind})
        except SystemExit as e:
            if e.code not in (0, None):
                report["user_script_error"] = "SystemExit(%r)" % e.code
                save_report(6)
                sys.exit(6)
        except Exception:
            report["user_script_error"] = traceback.format_exc()
            log(report["user_script_error"])
            save_report(6)
            sys.exit(6)
    if mode == "script":
        save_report(0)
        return

    frames = parse_frames(spec.get("frames"), scene)
    name = spec.get("output_name", "frame")
    failed = 0
    log("rendering %d frame(s): %s .. %s" % (len(frames), frames[0], frames[-1]))
    for i, f in enumerate(frames):
        final = os.path.join(out_dir, "%s_%04d%s" % (name, f, ext))
        rec = {"frame": f, "file": os.path.basename(final)}
        if os.path.exists(final) and os.path.getsize(final) > 0:
            rec["status"] = "skipped_existing"
            report["frames"].append(rec)
            continue
        part = os.path.join(out_dir, ".%s_%04d.partial%s" % (name, f, ext))
        t0 = time.time()
        try:
            scene.frame_set(f)
            result = bpy.ops.render.render(write_still=False, scene=scene.name)
            if "FINISHED" not in result:
                raise RuntimeError("render returned %s" % result)
            bpy.data.images["Render Result"].save_render(filepath=part, scene=scene)
            if not os.path.exists(part) or os.path.getsize(part) == 0:
                raise RuntimeError("no image written")
            os.replace(part, final)
            rec["status"] = "ok"
        except Exception as e:
            failed += 1
            rec["status"] = "failed"
            rec["error"] = str(e)
            log("frame %d FAILED: %s" % (f, e))
            if os.path.exists(part):
                os.remove(part)
        rec["seconds"] = round(time.time() - t0, 2)
        report["frames"].append(rec)
        log("frame %d done (%d/%d) %.1fs %s" % (f, i + 1, len(frames), rec["seconds"], rec["status"]))
        save_report(None)
    code = 3 if failed else 0
    save_report(code)
    sys.exit(code)


try:
    main()
except SystemExit:
    raise
except Exception:
    traceback.print_exc()
    sys.exit(7)
