"""Vast.ai provider, implemented on the `vastai` CLI."""
import json
import os
import re
import shutil
import subprocess

from .base import Provider, ProviderError

# Test hook: tests/mock_vastai.py stands in for the real CLI.
VASTAI = os.environ.get("BGPU_VASTAI", "vastai")

# Vast actual_status -> normalized status
_STATUS = {
    None: "starting", "created": "starting", "loading": "starting", "rebooting": "starting",
    "running": "running",
    "stopped": "stopped", "frozen": "stopped",
    "exited": "failed", "offline": "failed", "unknown": "failed",
}


class VastProvider(Provider):
    name = "vast"
    console_url = "https://console.vast.ai/instances/"

    def _cli(self, *args, timeout=120, check=True):
        cmd = [VASTAI] + list(args) + ["--raw"]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ProviderError("vastai %s timed out" % args[0])
        except FileNotFoundError:
            raise ProviderError("vastai CLI not found (looked for %r)" % VASTAI)
        out = p.stdout.strip()
        if check and p.returncode != 0:
            raise ProviderError("vastai %s failed: %s" % (" ".join(args[:2]), (p.stderr or out)[-500:]))
        try:
            # strict=False: Vast embeds raw control characters (docker pull
            # progress) in status_msg while an instance is loading.
            return json.loads(out, strict=False) if out else None
        except ValueError:
            if check:
                raise ProviderError("vastai %s returned non-JSON: %s" % (" ".join(args[:2]), out[-300:]))
            return out

    # -- account ---------------------------------------------------------
    def account(self):
        user = self._cli("show", "user") or {}
        return {"id": user.get("id"), "credit": float(user.get("credit") or 0)}

    def ssh_key_registered(self, public_key_text):
        parts = public_key_text.split()
        body = parts[1] if len(parts) > 1 else public_key_text.strip()
        keys = self._cli("show", "ssh-keys") or []
        return any(body in (k.get("public_key") or "") for k in keys)

    # -- offers ----------------------------------------------------------
    def search_offers(self, policy, disk_gb):
        # Vast applies dph_total<= at its 5 GB default disk, but returns prices
        # for --storage. The controller re-checks the cap on the returned price.
        # A "16 GB" machine reports ~15.6 GB usable, so allow the same 0.5 GB
        # slack the controller uses instead of dropping those offers here.
        q = ("verified=true rentable=true external=false num_gpus=1 "
             "gpu_ram>=%g cpu_ram>=%g dph_total<=%.4f reliability>=%.3f "
             "direct_port_count>=1 inet_down>=%d" % (
                 policy["min_gpu_ram_gb"] - 0.5, policy["min_cpu_ram_gb"] - 0.5, policy["max_dph_total"],
                 policy["min_reliability"], policy["min_inet_down_mbps"]))
        raw = self._cli("search", "offers", q, "-o", "dph_total", "--storage", str(disk_gb),
                        "--limit", "200") or []
        return [self._offer(o) for o in raw]

    @staticmethod
    def _offer(o):
        name = str(o.get("gpu_name") or "")
        return {
            "offer_id": o["id"], "machine_id": o.get("machine_id"), "gpu": name,
            "nvidia": name.startswith(("RTX", "GTX", "Tesla", "Quadro", "A", "H", "L", "Titan")) or "NVIDIA" in name,
            "num_gpus": int(o.get("num_gpus") or 1),
            "vram_gb": round((o.get("gpu_ram") or 0) / 1024.0, 1), "ram_gb": round((o.get("cpu_ram") or 0) / 1024.0, 1),
            "dph_total": round(float(o.get("dph_total") or 99), 4), "driver": o.get("driver_version"),
            "compute_cap": o.get("compute_cap"),
            "reliability": round(float(o.get("reliability2") or o.get("reliability") or 0), 3),
            "verified": bool(o.get("verified", o.get("verification") == "verified")),
            "location": o.get("geolocation"), "cpu_arch": o.get("cpu_arch") or "amd64",
            "inet_down_mbps": round(o.get("inet_down") or 0), "inet_up_mbps": round(o.get("inet_up") or 0),
            "down_per_tb": round(float(o.get("internet_down_cost_per_tb") or 0), 2),
            "up_per_tb": round(float(o.get("internet_up_cost_per_tb") or 0), 2),
        }

    # -- instances -------------------------------------------------------
    def create_instance(self, offer_id, image, disk_gb, label, env, onstart):
        env_args = " ".join("-e %s=%s" % (k, v) for k, v in env.items())
        res = self._cli("create", "instance", str(offer_id), "--image", image, "--disk", str(disk_gb),
                        "--ssh", "--direct", "--label", label, "--env", env_args,
                        "--onstart-cmd", onstart, "--cancel-unavail")
        iid = (res or {}).get("new_contract")
        if not (res or {}).get("success") or not iid:
            raise ProviderError("create rejected: %s" % {k: v for k, v in (res or {}).items() if "key" not in k})
        return iid

    def list_instances(self):
        res = self._cli("show", "instances")
        return [self._instance(i) for i in res] if isinstance(res, list) else []

    @staticmethod
    def _instance(i):
        status = _STATUS.get(i.get("actual_status"), "failed")
        if i.get("intended_status") == "stopped" and status != "failed":
            status = "stopped"
        return {
            "id": i["id"], "label": i.get("label"), "status": status,
            "status_detail": "%s %s" % (i.get("actual_status"), (i.get("status_msg") or "").strip()),
            "started_at": i.get("start_date"), "ssh_host": i.get("ssh_host"), "ssh_port": i.get("ssh_port"),
            "gpu": i.get("gpu_name"), "dph_total": i.get("dph_total"),
        }

    def ssh_endpoint(self, instance):
        url = self._cli("ssh-url", str(instance["id"]), check=False)
        m = re.match(r"ssh://(?:[^@]+@)?([^:/]+):(\d+)", str(url or "").strip())
        if m:
            return m.group(1), int(m.group(2))
        return Provider.ssh_endpoint(self, instance)

    def destroy_instance(self, instance_id):
        self._cli("destroy", "instance", str(instance_id), "-y", check=False)

    # -- on-instance runtime limit ----------------------------------------
    def watchdog_hooks(self):
        # Vast injects CONTAINER_ID and a per-instance CONTAINER_API_KEY that
        # may stop/destroy only this instance.
        with open(os.path.join(os.path.dirname(__file__), "vast_hooks.sh")) as f:
            return f.read()

    # -- local environment -------------------------------------------------
    def doctor_checks(self):
        return [(shutil.which(VASTAI) is not None, "vastai CLI found",
                 "install: curl -fsSL https://vast.ai/install.sh | bash")]

    def path_dirs(self):
        found = shutil.which(VASTAI)
        return [os.path.dirname(found)] if found else []


PROVIDER = VastProvider
