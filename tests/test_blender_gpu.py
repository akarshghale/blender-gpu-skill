#!/usr/bin/env python3
"""End-to-end tests for blender-gpu against a mock Vast.ai + mock SSH + fake
Blender. Spends nothing and never touches the real Vast account.

    python3 ~/.claude/skills/blender/tests/test_blender_gpu.py -v
"""
import hashlib
import http.server
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
TOOL = os.path.dirname(HERE)
CLI = os.path.join(TOOL, "scripts", "blender-gpu")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bgpu-test-")
        self.mock = os.path.join(self.tmp, "mock")
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(os.path.join(self.mock, "remote"))
        os.makedirs(self.data)
        pub = os.path.join(self.tmp, "id_test.pub")
        open(pub, "w").write("ssh-ed25519 AAAATESTKEYONLY test\n")
        json.dump({"ssh_private_key": pub[:-4], "plan_valid_minutes": 30, "approval": "flag"},
                  open(os.path.join(self.data, "config.json"), "w"))
        self.env = dict(os.environ, MOCK_ROOT=self.mock, MOCK_PUBKEY=pub, BGPU_DATA_DIR=self.data,
                        BGPU_VASTAI=os.path.join(HERE, "mock_vastai.py"), BGPU_SSH=os.path.join(HERE, "mock_ssh.py"),
                        MOCK_BLENDER=os.path.join(HERE, "fake_blender"), BGPU_POLL_SECONDS="0.2")
        self.blend = os.path.join(self.tmp, "proj", "scene.blend")
        os.makedirs(os.path.dirname(self.blend))
        open(self.blend, "wb").write(b"BLENDER-v405RENDB" + os.urandom(2048))
        os.makedirs(os.path.join(self.tmp, "proj", "textures"))
        open(os.path.join(self.tmp, "proj", "textures", "wood.jpg"), "wb").write(b"\xff\xd8\xff" + os.urandom(500))

    def tearDown(self):
        subprocess.run(["pkill", "-f", self.mock], capture_output=True)  # stray watchdogs
        if not os.environ.get("BGPU_TEST_KEEP"):
            shutil.rmtree(self.tmp, ignore_errors=True)

    def cli(self, *args, env=None, timeout=120):
        e = dict(self.env, **(env or {}))
        p = subprocess.run([sys.executable, CLI] + list(args), capture_output=True, text=True, env=e, timeout=timeout)
        return p.returncode, p.stdout + p.stderr

    def plan(self, *extra, env=None):
        rc, out = self.cli("plan", self.blend, *extra, env=env)
        self.assertEqual(rc, 0, out)
        job = [l.split()[1] for l in out.splitlines() if l.startswith("JOB ")][0]
        return job, out

    def state(self):
        return json.load(open(os.path.join(self.mock, "state.json")))

    def calls(self, verb=None):
        p = os.path.join(self.mock, "calls.log")
        rows = [json.loads(l) for l in open(p)] if os.path.exists(p) else []
        return [r for r in rows if verb is None or " ".join(r[:2]) == verb]

    def job(self, jid):
        return json.load(open(os.path.join(self.data, "jobs", jid, "job.json")))


class TestPolicyAndApproval(Base):
    def test_search_filters_policy(self):
        rc, out = self.cli("search")
        self.assertEqual(rc, 0, out)
        ids = [l.split()[0] for l in out.splitlines() if l[:1].isdigit() and "offers meet" not in l]
        self.assertEqual(ids, ["101", "102", "105"], out)  # RTX first by price, then V100
        # 103 passes Vast's 5 GB filter but exceeds $0.10 with the real 20 GB disk
        self.assertNotIn("103", ids)
        self.assertNotIn("104", ids)  # driver 535 < 575
        self.assertNotIn("106", ids)  # over cap

    def test_plan_never_rents(self):
        job, out = self.plan("--frames", "1-3")
        self.assertIn("Nothing has been rented", out)
        self.assertEqual(self.calls("create instance"), [])
        j = self.job(job)
        self.assertEqual(j["state"], "planned")
        self.assertEqual([c["offer_id"] for c in j["candidates"]], [101, 102, 105])
        self.assertLessEqual(max(c["dph_total"] for c in j["candidates"]), 0.10)

    def test_run_requires_approve(self):
        job, _ = self.plan()
        rc, out = self.cli("run", job)
        self.assertEqual(rc, 2, out)
        self.assertIn("requires --approve", out)
        self.assertEqual(self.calls("create instance"), [])

    def test_insufficient_credit_blocks_rental(self):
        job, _ = self.plan()
        rc, out = self.cli("run", job, "--approve", env={"MOCK_CREDIT": "0"})
        self.assertNotEqual(rc, 0)
        self.assertIn("Nothing was rented", out)
        self.assertEqual(self.calls("create instance"), [])

    def test_expired_plan(self):
        job, _ = self.plan()
        p = os.path.join(self.data, "jobs", job, "job.json")
        j = json.load(open(p))
        j["plan_expires"] = time.time() - 1
        json.dump(j, open(p, "w"))
        rc, out = self.cli("run", job, "--approve")
        self.assertIn("plan expired", out)
        self.assertEqual(self.calls("create instance"), [])


class TestJobLifecycle(Base):
    def test_happy_path(self):
        job, _ = self.plan("--frames", "1-3", "--project-dir", os.path.dirname(self.blend))
        rc, out = self.cli("run", job, "--approve")
        self.assertEqual(rc, 0, out)
        j = self.job(job)
        self.assertEqual(j["state"], "verified", out)
        outdir = os.path.join(self.data, "jobs", job, "output")
        self.assertEqual(sorted(os.listdir(outdir)), ["frame_0001.png", "frame_0002.png", "frame_0003.png"])
        # Downloaded files match the remote manifest
        for line in open(os.path.join(self.data, "jobs", job, "MANIFEST.sha256")):
            digest, rel = line.split()
            self.assertEqual(hashlib.sha256(open(os.path.join(outdir, rel), "rb").read()).hexdigest(), digest)
        # Created with the right policy, then destroyed, registry cleared
        create = self.calls("create instance")[0]
        self.assertEqual(create[2], "101")
        self.assertIn("NVIDIA_DRIVER_CAPABILITIES=all", create[create.index("--env") + 1])
        self.assertTrue(create[create.index("--label") + 1].startswith("bgpu-"))
        self.assertEqual(self.state()["instances"], [])
        self.assertTrue(j["destroy_confirmed"])
        self.assertEqual(os.listdir(os.path.join(self.data, "state", "active")), [])
        # OptiX chosen, report captured
        self.assertEqual(j["result"]["device"]["backend"], "OPTIX")
        self.assertEqual(j["result"]["frames_ok"], 3)
        # Watchdog was armed on the "instance" with a deadline ~120 min after creation
        self.assertIn("watchdog: armed", out)
        self.assertAlmostEqual(j["deadline"] - j["instance_created"], 7200, delta=1)
        # Texture was uploaded with the project directory
        self.assertIn("./textures/wood.jpg", open(os.path.join(self.data, "jobs", job, "logs", "upload.sha256")).read())

    def test_cuda_fallback(self):
        job, _ = self.plan()
        rc, out = self.cli("run", job, "--approve", env={"FAKE_GPU": "cuda"})
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.job(job)["result"]["device"]["backend"], "CUDA")

    def test_no_gpu_fails_and_destroys(self):
        job, _ = self.plan()
        rc, out = self.cli("run", job, "--approve", env={"FAKE_GPU": "none"})
        self.assertNotEqual(rc, 0)
        self.assertIn("no OptiX/CUDA device", out)
        self.assertEqual(self.state()["instances"], [])
        self.assertEqual(self.job(job)["state"], "failed")

    def test_boot_failure_tries_next_candidate(self):
        job, _ = self.plan()
        rc, out = self.cli("run", job, "--approve", env={"MOCK_BOOT": "101"})
        self.assertEqual(rc, 0, out)
        created = [c[2] for c in self.calls("create instance")]
        self.assertEqual(created, ["101", "102"])
        self.assertEqual(sorted(self.state()["destroyed"]), [9000, 9001])
        self.assertEqual(self.state()["instances"], [])

    def test_create_timeout_orphan_is_destroyed(self):
        job, _ = self.plan()
        rc, out = self.cli("run", job, "--approve", env={"MOCK_CREATE_TIMEOUT_OFFER": "101"})
        self.assertEqual(rc, 0, out)
        self.assertIn("found instance 9000 from the failed create", out)
        self.assertIn(9000, self.state()["destroyed"])
        self.assertEqual(self.state()["instances"], [])

    def test_partial_render_downloads_then_destroys(self):
        job, _ = self.plan("--frames", "1-4")
        rc, out = self.cli("run", job, "--approve", env={"FAKE_FAIL_FRAMES": "3"})
        self.assertEqual(rc, 3, out)
        j = self.job(job)
        self.assertEqual(j["state"], "partial")
        self.assertEqual(j["result"]["frames_failed"], [3])
        self.assertEqual(sorted(os.listdir(os.path.join(self.data, "jobs", job, "output"))),
                         ["frame_0001.png", "frame_0002.png", "frame_0004.png"])
        self.assertEqual(self.state()["instances"], [])

    def test_missing_textures_fail_fast(self):
        job, _ = self.plan()  # only the .blend is uploaded
        rc, out = self.cli("run", job, "--approve", env={"FAKE_MISSING": "1"})
        self.assertNotEqual(rc, 0)
        log = open(os.path.join(self.data, "jobs", job, "logs", "remote", "blender.log")).read()
        self.assertIn("MISSING image wood.jpg", log)
        self.assertEqual(self.state()["instances"], [])

    def test_transient_corruption_is_retried(self):
        job, _ = self.plan("--frames", "1-2")
        rc, out = self.cli("run", job, "--approve", env={"MOCK_CORRUPT_DOWNLOAD": "1"})
        self.assertEqual(rc, 0, out)
        self.assertIn("checksum mismatch", out)
        self.assertEqual(self.job(job)["state"], "verified")
        self.assertEqual(self.state()["instances"], [])

    def test_persistent_corruption_keeps_instance(self):
        job, _ = self.plan("--frames", "1-2")
        rc, out = self.cli("run", job, "--approve", env={"MOCK_CORRUPT_DOWNLOAD": "99"})
        self.assertEqual(rc, 4, out)
        j = self.job(job)
        self.assertEqual(j["state"], "needs_attention")
        self.assertEqual(len(self.state()["instances"]), 1, "instance must NOT be destroyed with unverified outputs")
        self.assertFalse(os.path.exists(os.path.join(self.data, "jobs", job, "output")))
        # recovery path: fetch succeeds once the link is healthy, then destroy
        rc, out = self.cli("fetch", job, "--destroy", env={"MOCK_CORRUPT_DOWNLOAD": "0"})
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.state()["instances"], [])
        self.assertEqual(len(os.listdir(os.path.join(self.data, "jobs", job, "output"))), 2)

    def test_flaky_destroy_is_confirmed(self):
        job, _ = self.plan()
        rc, out = self.cli("run", job, "--approve", env={"MOCK_DESTROY_FAILS": "2"})
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.state()["instances"], [])
        self.assertGreaterEqual(len(self.calls("destroy instance")), 3)

    def test_sigterm_mid_render_salvages_and_destroys(self):
        job, _ = self.plan("--frames", "1-40")
        e = dict(self.env, FAKE_FRAME_SECONDS="0.3")
        p = subprocess.Popen([sys.executable, CLI, "run", job, "--approve"], env=e,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for _ in range(200):
            time.sleep(0.2)
            j = self.job(job)
            outdir = os.path.join(self.mock, "remote", "9000", "job", "output")
            if j["state"] == "rendering" and os.path.isdir(outdir) and len(os.listdir(outdir)) >= 3:
                break
        p.send_signal(signal.SIGTERM)
        out, _ = p.communicate(timeout=120)
        self.assertIn("interrupted", out)
        self.assertEqual(self.state()["instances"], [], out)
        self.assertEqual(self.job(job)["state"], "interrupted")
        # frames finished before the interrupt were salvaged and verified
        self.assertGreaterEqual(len(os.listdir(os.path.join(self.data, "jobs", job, "output"))), 3)

    def test_script_mode(self):
        script = os.path.join(self.tmp, "make.py")
        open(script, "w").write("import os\nopen(os.path.join(BGPU_OUTPUT_DIR,'stats.json'),'w').write('{\"device\":\"%s\"}' % BGPU_DEVICE)\n")
        job, _ = self.plan("--mode", "script", "--script", script)
        rc, out = self.cli("run", job, "--approve")
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.load(open(os.path.join(self.data, "jobs", job, "output", "stats.json"))), {"device": "OPTIX"})

    def test_script_mode_without_blend(self):
        script = os.path.join(self.tmp, "gen_scene.py")
        open(script, "w").write("import os\nopen(os.path.join(BGPU_OUTPUT_DIR,'blend.txt'),'w')"
                                ".write(os.environ.get('FAKE_BLEND_PATH','?') or 'none')\n")
        rc, out = self.cli("plan", "--mode", "script", "--script", script)
        self.assertEqual(rc, 0, out)
        self.assertIn("none (Blender's default scene)", out)
        job = [l.split()[1] for l in out.splitlines() if l.startswith("JOB ")][0]
        self.assertTrue(job.endswith("-gen-scene"), job)
        self.assertEqual(list(self.job(job)["inputs"]), ["_extra/gen_scene.py"])
        rc, out = self.cli("run", job, "--approve")
        self.assertEqual(rc, 0, out)
        # Blender was started with no file argument.
        self.assertEqual(open(os.path.join(self.data, "jobs", job, "output", "blend.txt")).read(), "none")

    def test_blend_required_outside_script_mode(self):
        script = os.path.join(self.tmp, "make.py")
        open(script, "w").write("pass\n")
        for args in ([], ["--frames", "1-3"], ["--script", script], ["--mode", "script"]):
            rc, out = self.cli("plan", *args)
            self.assertNotEqual(rc, 0, args)
            self.assertIn("a .blend is required", out)
        self.assertEqual(self.calls("create instance"), [])


class TestRuntimeLimit(Base):
    def put_instances(self, insts):
        json.dump({"instances": insts, "next_id": 9500}, open(os.path.join(self.mock, "state.json"), "w"))

    def test_reaper(self):
        t = time.time()
        self.put_instances([
            {"id": 1, "label": "bgpu-old", "actual_status": "running", "start_date": t - 121 * 60},
            {"id": 2, "label": "bgpu-young", "actual_status": "running", "start_date": t - 10 * 60},
            {"id": 3, "label": "someone-else", "actual_status": "running", "start_date": t - 900 * 60},
            {"id": 4, "label": None, "actual_status": "loading", "start_date": None,  # ours via registry
             "status_msg": "pulling<TAB>layer<CR>"},  # raw control chars, as real Vast returns
        ])
        os.makedirs(os.path.join(self.data, "state", "active"), exist_ok=True)
        json.dump({"instance_id": 4, "created": t - 130 * 60},
                  open(os.path.join(self.data, "state", "active", "4.json"), "w"))
        rc, out = self.cli("reap", "--dry-run")
        self.assertEqual(sorted(i["id"] for i in self.state()["instances"]), [1, 2, 3, 4])
        rc, out = self.cli("reap")
        self.assertEqual(rc, 0, out)
        self.assertEqual(sorted(i["id"] for i in self.state()["instances"]), [2, 3], out)
        self.assertTrue(os.path.exists(os.path.join(self.data, "logs", "reaper.heartbeat")))

    def test_panic(self):
        self.put_instances([{"id": 1, "label": "bgpu-a", "start_date": time.time()},
                            {"id": 2, "label": "bgpu-b", "start_date": time.time()},
                            {"id": 3, "label": "other", "start_date": time.time()}])
        rc, out = self.cli("panic")  # non-interactive without -y refuses
        self.assertEqual(rc, 2)
        self.assertEqual(len(self.state()["instances"]), 3)
        rc, out = self.cli("panic", "-y")
        self.assertEqual(rc, 0, out)
        self.assertEqual([i["id"] for i in self.state()["instances"]], [3])
        rc, out = self.cli("panic", "-y", "--all")
        self.assertEqual(self.state()["instances"], [])

    def test_instance_watchdog_destroys_itself(self):
        """Run the real onstart.sh with a deadline 2s away against a fake Vast API."""
        hits = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_DELETE(self):
                hits.append((self.path, self.headers.get("Authorization")))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"success": true}')

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        root = os.path.join(self.mock, "remote", "777")
        os.makedirs(os.path.join(root, "status"))
        # a stand-in "render" in its own process group that must be killed
        job = subprocess.Popen(["sleep", "300"])
        open(os.path.join(root, "status", "render.pid"), "w").write(str(job.pid))
        rc, script = self.cli("_onstart", str(int(time.time()) + 2))
        self.assertEqual(rc, 0, script)
        self.assertNotIn("__PROVIDER_HOOKS__", script)
        env = dict(os.environ, BGPU_REMOTE_ROOT=root, CONTAINER_ID="777", CONTAINER_API_KEY="instkey",
                   BGPU_API_BASE="http://127.0.0.1:%d/api/v0" % srv.server_port)
        subprocess.run(["bash", "-c", script], env=env, timeout=10)
        for _ in range(100):
            if hits:
                break
            time.sleep(0.2)
        srv.shutdown()
        self.assertEqual(hits, [("/api/v0/instances/777/", "Bearer instkey")])
        time.sleep(1)
        self.assertIsNotNone(job.poll(), "render process should have been killed")
        self.assertIn("runtime limit reached", open(os.path.join(root, "watchdog.log")).read())


class TestHarnessNeutral(Base):
    """JSON output and the human approval gate that does not rely on any harness."""

    def set_approval(self, mode):
        p = os.path.join(self.data, "config.json")
        c = json.load(open(p))
        c["approval"] = mode
        json.dump(c, open(p, "w"))

    def jcli(self, *args, env=None):
        e = dict(self.env, **(env or {}))
        p = subprocess.run([sys.executable, CLI] + list(args), capture_output=True, text=True, env=e,
                           timeout=120, start_new_session=True)  # no controlling terminal, like an agent
        lines = p.stdout.strip().splitlines()
        self.assertEqual(len(lines), 1, "stdout must be exactly one JSON line: %r\nstderr: %s" % (p.stdout, p.stderr))
        return p.returncode, json.loads(lines[0]), p.stderr

    def test_json_everywhere(self):
        rc, j, _ = self.jcli("--json", "search")
        self.assertEqual((rc, j["ok"], j["total"]), (0, True, 3))
        self.assertEqual([o["offer_id"] for o in j["offers"]], [101, 102, 105])
        rc, j, err = self.jcli("plan", self.blend, "--frames", "1-2", "--json")  # flag after subcommand
        self.assertEqual(rc, 0, err)
        self.assertIn("JOB ", err)  # human text went to stderr
        job = j["job_id"]
        self.assertEqual(j["run_command"], "blender-gpu run %s --approve --detach" % job)
        self.assertEqual(j["approval_mode"], "flag")
        rc, j, _ = self.jcli("run", job, "--json")
        self.assertEqual((rc, j["error"]), (2, "--approve required"))
        rc, j, err = self.jcli("run", job, "--approve", "--json")
        self.assertEqual((rc, j["state"], j["destroy_confirmed"]), (0, "verified", True), err)
        self.assertEqual(j["summary"]["frames_ok"], 2)
        self.assertTrue(os.path.isdir(j["output_dir"]))
        rc, j, _ = self.jcli("status", job, "--json")
        self.assertEqual(j["job"]["state"], "verified")
        rc, j, _ = self.jcli("status", "--json")
        self.assertEqual((j["jobs"][-1]["job_id"], j["instances"]), (job, []))
        rc, j, _ = self.jcli("panic", "-y", "--json")
        self.assertEqual((rc, j["destroyed"], j["remaining"]), (0, [], []))
        rc, j, _ = self.jcli("doctor", "--json")
        self.assertTrue(any(c["check"].startswith("vast API key valid") for c in j["checks"]))
        rc, j, _ = self.jcli("run", "no-such-job", "--approve", "--json")
        self.assertEqual((rc, j["ok"]), (1, False))
        self.assertIn("no such job", j["error"])

    def test_tty_mode_refuses_without_terminal(self):
        self.set_approval("tty")
        job, _ = self.plan()
        rc, j, _ = self.jcli("run", job, "--approve", "--json")
        self.assertEqual(rc, 1)
        self.assertIn("no terminal", j["error"])
        self.assertEqual(self.calls("create instance"), [])

    def test_forged_nonce_does_not_bypass_approval(self):
        self.set_approval("tty")
        job, _ = self.plan()
        p = os.path.join(self.data, "jobs", job, "job.json")
        d = json.load(open(p))
        d["approval"] = {"via": "dialog", "at": time.time(), "nonce_sha256": "0" * 64}
        json.dump(d, open(p, "w"))
        rc, j, _ = self.jcli("run", job, "--approve", "--json", env={"BGPU_APPROVAL_NONCE": "guess"})
        self.assertEqual(rc, 1)
        self.assertEqual(self.calls("create instance"), [])

    def test_detach_inherits_approval_once(self):
        job, _ = self.plan("--frames", "1")
        rc, j, err = self.jcli("run", job, "--approve", "--detach", "--json")
        self.assertEqual((rc, j["state"], j["approved_via"]), (0, "started", "flag"), err)
        for _ in range(300):
            if self.job(job)["state"] in ("verified", "failed", "partial", "needs_attention"):
                break
            time.sleep(0.2)
        d = self.job(job)
        self.assertEqual(d["state"], "verified", open(j["log"]).read())
        self.assertNotIn("nonce_sha256", d["approval"])  # one-time token consumed
        self.assertEqual(self.state()["instances"], [])

    def test_dialog_gate_in_process(self):
        """Load the controller as a module and stub only the dialog itself."""
        import importlib.machinery
        import importlib.util
        loader = importlib.machinery.SourceFileLoader("bgpu_cli", CLI)
        spec = importlib.util.spec_from_loader("bgpu_cli", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        job, _ = self.plan()

        class J:
            data = self.job(job)
            events = []

            def event(self, state, **kw):
                self.events.append(state)
        seen = []
        mod._ask_dialog = lambda text: seen.append(text) or False
        with self.assertRaises(mod.Fail):
            mod.human_approval({"approval": "dialog"}, J())
        self.assertIn("Worst-case cost", seen[0])
        self.assertIn("RTX 3060", seen[0])
        self.assertEqual(J.events, ["declined"])
        mod._ask_dialog = lambda text: True
        self.assertEqual(mod.human_approval({"approval": "dialog"}, J()), "dialog")
        with self.assertRaises(mod.Fail):
            mod.human_approval({"approval": "yolo"}, J())


class TestProviderInterface(Base):
    def setUp(self):
        super().setUp()
        sys.path.insert(0, TOOL)
        os.environ.update(BGPU_VASTAI=self.env["BGPU_VASTAI"], MOCK_ROOT=self.mock, MOCK_PUBKEY=self.env["MOCK_PUBKEY"])
        import importlib
        import providers.vast
        importlib.reload(providers.vast)  # pick up BGPU_VASTAI
        self.providers = importlib.import_module("providers")
        self.base = importlib.import_module("providers.base")
        self.vast = providers.vast

    def tearDown(self):
        for k in ("BGPU_VASTAI", "MOCK_ROOT", "MOCK_PUBKEY"):
            os.environ.pop(k, None)
        super().tearDown()

    def test_vast_implements_every_method(self):
        missing = [n for n, f in vars(self.base.Provider).items()
                   if callable(f) and not n.startswith("_") and "NotImplementedError" in __import__("inspect").getsource(f)
                   and getattr(self.vast.VastProvider, n) is f]
        self.assertEqual(missing, [])

    def test_get_provider_from_config(self):
        p = self.providers.get_provider({"provider": "vast"})
        self.assertEqual(p.name, "vast")
        with self.assertRaises(self.providers.ProviderError):
            self.providers.get_provider({"provider": "nope"})

    def test_normalized_records(self):
        p = self.vast.VastProvider({})
        offers = p.search_offers({"min_gpu_ram_gb": 12, "min_cpu_ram_gb": 16, "max_dph_total": 0.1,
                                  "min_reliability": 0.95, "min_inet_down_mbps": 200}, 20)
        keys = {"offer_id", "machine_id", "gpu", "nvidia", "num_gpus", "vram_gb", "ram_gb", "dph_total", "driver",
                "compute_cap", "reliability", "verified", "location", "cpu_arch", "inet_down_mbps", "inet_up_mbps",
                "down_per_tb", "up_per_tb"}
        for o in offers:
            self.assertEqual(set(o), keys)
        o101 = [o for o in offers if o["offer_id"] == 101][0]
        self.assertEqual((o101["vram_gb"], o101["nvidia"], o101["verified"]), (12.0, True, True))
        self.assertAlmostEqual(o101["dph_total"], 0.06 + 0.2 * 20 / 720, places=4)
        iid = p.create_instance(101, "img", 20, "bgpu-x", {"A": "1"}, "true")
        insts = p.list_instances()
        self.assertEqual(set(insts[0]), {"id", "label", "status", "status_detail", "started_at", "ssh_host",
                                         "ssh_port", "gpu", "dph_total"})
        self.assertIn(insts[0]["status"], self.base.STATUSES)
        self.assertEqual(p.get_instance(iid)["status"], "running")  # mock boots on 2nd poll
        self.assertEqual(p.ssh_endpoint(p.get_instance(iid)), ("127.0.0.1", iid))
        p.destroy_instance(iid)
        self.assertIsNone(p.get_instance(iid))

    def test_vast_status_mapping(self):
        f = self.vast.VastProvider._instance
        cases = {None: "starting", "loading": "starting", "running": "running", "exited": "failed",
                 "offline": "failed", "unknown": "failed", "stopped": "stopped", "weird-new-state": "failed"}
        for raw, want in cases.items():
            self.assertEqual(f({"id": 1, "actual_status": raw})["status"], want, raw)
        self.assertEqual(f({"id": 1, "actual_status": "running", "intended_status": "stopped"})["status"], "stopped")

    def test_controller_has_no_provider_specifics(self):
        code = open(CLI).read().split('"""', 2)[2]  # skip module docstring
        for token in ("vastai", "actual_status", "gpu_name", "start_date", "new_contract", "CONTAINER_API_KEY"):
            self.assertNotIn(token, code)
        tmpl = open(os.path.join(TOOL, "remote", "onstart.sh")).read()
        self.assertNotIn("CONTAINER_API_KEY", tmpl)
        # each placeholder exactly once, or substitution corrupts the script
        self.assertEqual((tmpl.count("__DEADLINE__"), tmpl.count("__PROVIDER_HOOKS__")), (1, 1))
        rc, script = self.cli("_onstart", "1234567890")
        self.assertEqual(subprocess.run(["bash", "-n"], input=script, text=True).returncode, 0, "rendered onstart has a syntax error")


class TestRenderDriver(Base):
    """render.py directly against the fake bpy."""

    def run_render(self, spec, env=None):
        out = os.path.join(self.tmp, "out")
        spec = dict({"output_dir": out, "report_path": os.path.join(self.tmp, "report.json")}, **spec)
        sp = os.path.join(self.tmp, "spec.json")
        json.dump(spec, open(sp, "w"))
        e = dict(os.environ, **(env or {}))
        p = subprocess.run([os.path.join(HERE, "fake_blender"), "-b", "--factory-startup", self.blend,
                            "--python", os.path.join(TOOL, "remote", "render.py"), "--", sp],
                           capture_output=True, text=True, env=e)
        return p.returncode, p.stdout + p.stderr, out

    def test_frame_parsing_and_resume(self):
        rc, log, out = self.run_render({"frames": "1-3,7", "format": "OPEN_EXR"})
        self.assertEqual(rc, 0, log)
        self.assertEqual(sorted(os.listdir(out)), ["frame_0001.exr", "frame_0002.exr", "frame_0003.exr", "frame_0007.exr"])
        rc, log, out = self.run_render({"frames": "1-3,7", "format": "OPEN_EXR"})
        rep = json.load(open(os.path.join(self.tmp, "report.json")))
        self.assertTrue(all(f["status"] == "skipped_existing" for f in rep["frames"]))

    def test_video_output_scene_switched_to_images(self):
        rc, log, out = self.run_render({"frames": "1"}, env={"FAKE_VIDEO_OUTPUT": "1"})
        self.assertEqual(rc, 0, log)

    def test_bad_frames_spec(self):
        rc, log, out = self.run_render({"frames": "5-2"})
        self.assertEqual(rc, 7, log)


if __name__ == "__main__":
    unittest.main()
