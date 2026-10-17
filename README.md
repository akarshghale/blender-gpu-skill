# blender-gpu

Render Blender files on a rented cloud NVIDIA GPU (Vast.ai), controlled from
your Mac. It plans and prices a job for free, rents one GPU only after you
click **Rent**, renders headless with Cycles (OptiX, CUDA fallback), downloads
and checksum-verifies the frames, then destroys the machine. Default limits:
$0.10/hr and 2 hours per job.

It is a plain CLI that works from a terminal, a script, or any agent. It is
also an agent skill: `SKILL.md` has the rules and the `--json` contract.

Requirements: macOS, `python3` 3.9+ (stdlib only), the
[Vast.ai CLI](https://vast.ai/install.sh) and a Vast account with credit.

```
Mac (controller)                          rented GPU (disposable worker)
scripts/blender-gpu ─ providers/<name>.py ──▶  create / destroy instance
       │                                   remote/onstart.sh   2-hour watchdog
       └──── SSH: upload, run, poll ─────▶  remote/bootstrap.sh pinned Blender + GPU probe
       ◀──── SSH: outputs + manifest ─────  remote/run_job.sh, render.py (OptiX → CUDA)
```

## Install

Clone into your agent's skills folder, or anywhere you like for CLI-only use:

```bash
git clone https://github.com/akarshghale/blender-gpu-skill ~/.claude/skills/blender
```

Then do the one-time account steps:

```bash
curl -fsSL https://vast.ai/install.sh | bash          # Vast CLI
vastai set api-key <KEY>                              # https://console.vast.ai/manage-keys/
vastai create ssh-key ~/.ssh/id_ed25519.pub           # key must have no passphrase (or use ssh-add)
```

Finally run `scripts/setup.sh` (free, safe to re-run). It puts `blender-gpu`
on your PATH (`~/.local/bin`), installs the launchd reaper and runs
`blender-gpu doctor`. See "Setup" in `SKILL.md` for details.

## Quick start

```bash
blender-gpu doctor                                   # setup checks (free)
blender-gpu plan ~/proj/scene.blend --project-dir ~/proj --frames 1-120
blender-gpu run <JOB> --approve --detach             # PAID: asks you in a dialog first
blender-gpu status <JOB>
open ~/BlenderGPU/jobs/<JOB>/output
```

`plan` options: `--frames 1-10,15|scene`, `--format PNG|OPEN_EXR|OPEN_EXR_MULTILAYER|JPEG|TIFF|WEBP`,
`--samples N`, `--resolution-percent N`, `--scene`, `--camera`, `--device auto|OPTIX|CUDA`,
`--mode script --script gen.py` (Blender Python; write files to `BGPU_OUTPUT_DIR`),
`--project-dir DIR` (textures and linked libraries), `--max-minutes N`, `--autoexec`,
`--allow-missing`, `--allow-cpu`.

Other commands: `search`, `fetch JOB [--destroy]`, `destroy JOB`, `reap [--dry-run]`,
`panic [-y] [--all]` (also `blender-gpu-panic`), `install-reaper`, `uninstall-reaper`.
Add `--json` to any command for one JSON object on stdout.

## Safety model

- **Approval:** `run` needs `--approve` plus a human approval chosen by
  `"approval"` in config:
  - `dialog` (default): a native macOS Rent / Don't-rent dialog. Times out
    to "don't rent" after 10 minutes.
  - `tty`: type `RENT` on the controlling terminal. Harnesses without a
    terminal cannot answer it.
  - `flag`: `--approve` alone. Use only if whatever runs the command already
    asks you before it does.
  No environment variable can change the mode.
- **Price:** an all-in $/hr cap (incl. disk) and a hardware policy, enforced
  by the controller on every offer.
- **Runtime:** a hard limit (default 120 min), enforced three ways:
  - the controller's own render budget;
  - an on-instance watchdog that destroys the instance via the provider's
    self-destroy hook;
  - a launchd reaper on this Mac.
- **Outputs:** the instance is only destroyed after every output matches the
  remote SHA-256 manifest. Unverifiable outputs keep the instance (bounded
  by the runtime limit) for `fetch`.

## Layout

```
<skill folder>/            the whole tool (self-contained)
  SKILL.md               rules + JSON contract for agents
  README.md              this file
  config.json            defaults
  scripts/               blender-gpu, blender-gpu-panic, setup.sh
  providers/             provider interface + vast.py
  remote/                scripts that run on the rented GPU
  tests/                 free offline tests

~/BlenderGPU/               data only (safe to delete jobs you no longer need)
  config.json            your overrides (optional; same keys as the defaults)
  jobs/<JOB>/            job.json, output/, logs/
  logs/                  reaper log/heartbeat
  cache/                 Blender tarball for the container test
  test-scenes/           sample scene
```

## Providers

`providers/base.py` defines the interface: normalized Offer and Instance
records, plus `account`, `search_offers`, `create_instance`, `list_instances`,
`ssh_endpoint`, `destroy_instance` and `watchdog_hooks`. `providers/vast.py`
is the only implementation. Select one with `"provider"` in config.

## Tests (free)

```bash
python3 tests/test_blender_gpu.py   # mock provider + SSH + fake Blender
tests/container_test.sh             # real Blender in a local Ubuntu container (CPU; Docker/OrbStack)
```

## Notes

- Your `.blend` and frames run on a third-party host's machine. Don't render
  confidential work on marketplace GPUs.
- You pay Vast directly. The limits above bound the cost of a job, but always
  check `blender-gpu status` and keep `blender-gpu-panic -y` in mind.

## License

MIT, see `LICENSE`.
