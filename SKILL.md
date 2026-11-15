---
name: blender
description: Render Blender files or run Blender Python on a rented cloud NVIDIA GPU (Vast.ai by default; headless Cycles with OptiX/CUDA), controlled from this Mac with the bundled `blender-gpu` CLI. Plans and prices jobs for free, rents only after the user approves, verifies outputs, destroys the instance. Use for "/blender", "render this on a GPU", "render frames on vast", or cleaning up blender-gpu instances.
---

# /blender

Everything this skill needs is in this folder:

| path | what |
|---|---|
| `scripts/blender-gpu` | the command (also on PATH as `blender-gpu`) |
| `scripts/blender-gpu-panic` | emergency stop (also on PATH) |
| `config.json` | defaults: price cap, runtime limit, Blender version, image |
| `providers/` | GPU provider interface + the Vast.ai implementation |
| `remote/` | scripts that run on the rented machine |
| `tests/` | free offline tests (no rental) |
| `README.md` | full human reference |

Job data (inputs, outputs, logs) lives in `~/BlenderGPU/`, never in this folder.
Per-user overrides go in `~/BlenderGPU/config.json` (same keys).

## Setup (once per Mac, free)

Run `blender-gpu doctor` first. If it prints `all good`, skip this section.

Requirements: macOS with `python3` 3.9+ (stdlib only), `ssh` and `tar`, all
built in. The rented GPU needs nothing: `remote/bootstrap.sh` installs
Blender there on every job.

1. Install the Vast CLI: `curl -fsSL https://vast.ai/install.sh | bash`.
2. The user sets their API key: `vastai set api-key <KEY>` (key from
   https://console.vast.ai/manage-keys/). Never ask for the key in chat.
3. Have an SSH key without a passphrase, or loaded with `ssh-add`. The
   default is `~/.ssh/id_ed25519`, created with
   `ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519`.
4. Register it with Vast: `vastai create ssh-key ~/.ssh/id_ed25519.pub`.
   This changes the user's account, so ask before running it.
5. Run `scripts/setup.sh`. It links `blender-gpu` and `blender-gpu-panic`
   into `~/.local/bin`, installs the launchd reaper that enforces the
   runtime limit, and runs `doctor`.
6. The account needs credit. `doctor` checks there is enough for one
   worst-case job.

Each failing `doctor` line prints its fix.

## Rules (they cost real money)

1. **Never rent without the user's explicit yes for this job.** Run `plan`
   first (free). Show the user the GPU, $/hr, worst-case cost and runtime
   limit, then ask. Only after a yes, run `run <job> --approve --detach`.
2. `run` also opens a **native macOS dialog** (Rent / Don't rent). That is the
   human gate, so tell the user to expect it. Do not bypass it: never click
   or script it, and never change `"approval"` in any config.json. If the
   user wants a different approval mode, they change it themselves.
3. Never call the provider CLI to create instances (`vastai create` or
   `vastai launch`). Never raise `max_dph_total` ($0.10/hr) or
   `max_runtime_minutes` (120) unless the user asks.
4. Never rent to test or debug the tool. Use `tests/` instead (free).
5. A yes covers one job. A re-plan, a retry or a new job needs a new yes.

## Workflow (use `--json`)

Every command accepts `--json`, before or after the subcommand. stdout is then
exactly one JSON object, and human-readable progress goes to stderr. The keys
`command`, `exit_code` and `ok` are always present, plus `error` on failure.

```bash
blender-gpu doctor --json                    # setup checks: {"checks":[{ok,check,hint}]}
blender-gpu search --json                    # {"offers":[...], "total":N}
blender-gpu plan scene.blend --project-dir DIR --frames 1-120 --json
#   -> {"job_id", "candidates":[{gpu,dph_total,...}], "worst_case_cost",
#       "max_runtime_minutes", "plan_expires", "approval_mode", "run_command"}
# ... show the user, ask, wait for yes ...
blender-gpu run JOB --approve --detach --json  # -> {"state":"started","pid","log"}
blender-gpu status JOB --json                # poll every 30-60 s: {"job":{"state",...}}
```

`plan` options:
- `--frames 1-10,15|scene`
- `--format PNG|OPEN_EXR|OPEN_EXR_MULTILAYER|JPEG|TIFF|WEBP`
- `--samples N` and `--resolution-percent N`
- `--scene` and `--camera`
- `--device auto|OPTIX|CUDA`
- `--script gen.py` runs Blender Python before rendering. Add `--mode script`
  to run only the script, which writes its files to `BGPU_OUTPUT_DIR`. In
  script mode the .blend is optional: `blender-gpu plan --mode script --script
  gen.py` starts Blender with its default scene.
- `--project-dir DIR` uploads textures and linked libraries with the .blend.
- `--max-minutes N` lowers the runtime limit for this job.
- `--autoexec`, `--allow-missing` and `--allow-cpu`.

## Rendering a scene you built (script mode)

To render a scene you create, write one Blender Python script that builds the
scene and renders it, then plan it with `plan --mode script --script gen.py`
(no .blend). Assume nothing exists on this Mac: no Blender, no `bpy`, no
assets. Never install or run Blender locally. The script must be
self-contained and work on its first GPU run. Only `python3 -m py_compile
gen.py` (syntax) can be checked locally.

In script mode the tool renders nothing itself. The script must:

- Build in `bpy.context.scene`. Only that scene has the GPU set up. A scene
  from `bpy.data.scenes.new()` renders on the CPU at GPU prices unless the
  script sets `scene.cycles.device = "GPU"` and makes it the active scene.
- Delete the default cube, camera and light first if it does not want them.
- Set a camera (`scene.camera`) and lighting, or the render is empty or black.
- Call the render: `bpy.ops.render.render(write_still=True)` for one image, or
  set `frame_start`/`frame_end` and use `animation=True`. `--frames` is ignored.
- Write every result into `BGPU_OUTPUT_DIR` (for example
  `scene.render.filepath = os.path.join(BGPU_OUTPUT_DIR, "frame_####")`).
  Anything else is lost when the instance is destroyed.
- Also save the built scene with
  `bpy.ops.wm.save_as_mainfile(filepath=os.path.join(BGPU_OUTPUT_DIR, "scene.blend"))`,
  so later re-renders can run as normal render jobs without rebuilding.
- Set samples, resolution and format in the script. The `plan` flags only reach
  `bpy.context.scene`, before the script runs.
- Use only built-in Blender features and the files it is given. The instance
  cannot reach this Mac. Upload assets with `--project-dir DIR`; the script
  finds them under `os.path.dirname(os.path.dirname(__file__))`.

A script error marks the job `failed` (traceback in `logs/remote/blender.log`)
and still costs the rental time used. For a long or expensive render, offer the
user a short, low-sample preview job first; it needs its own plan and yes.

Always use `--detach` for `run`. The job and its cleanup then survive your
process or session ending. Poll `status JOB --json` until `job.state` reaches
a terminal state:

| state | meaning | instance |
|---|---|---|
| `verified` | all frames rendered, downloaded, checksum-verified | destroyed |
| `partial` | some frames failed; finished ones verified locally | destroyed |
| `failed` | setup/render failed; logs (+ any verified frames) saved | destroyed |
| `interrupted` | stopped by a signal; finished frames salvaged | destroyed |
| `declined` | the human did not approve | never rented |
| `needs_attention` | outputs could not be verified | **kept** until the runtime limit |

For `needs_attention`, tell the user and offer either `blender-gpu fetch JOB --destroy`
(retries the download and destroys only after verification) or
`blender-gpu destroy JOB -y`.

Outputs: `~/BlenderGPU/jobs/<JOB>/output/`. Logs: `~/BlenderGPU/jobs/<JOB>/logs/`
(`remote/blender.log` has Blender's own output).

Exit codes of `run`: 0 verified, 1 failed or not approved, 2 `--approve` missing,
3 partial, 4 needs_attention, 5 destroy not confirmed (tell the user right away and
run `blender-gpu panic -y`), 130 interrupted.

## Emergency

`blender-gpu-panic -y` destroys every instance this tool created. Add `--all`
to destroy every instance on the account, but only when the user asks. The
2-hour limit is also enforced without any agent: a watchdog runs on the
instance, and a launchd job on this Mac runs `blender-gpu reap` every 5 minutes.
