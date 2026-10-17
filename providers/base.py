"""GPU provider interface for blender-gpu.

The controller (bin/blender-gpu) talks to a GPU marketplace only through a
`Provider`. It never sees provider-specific field names or commands. A
provider translates between its own API and these normalized records:

Offer (dict), one rentable machine. Prices are USD.
    offer_id        provider's id to pass to create_instance
    machine_id      stable host id, if the provider has one (else None)
    gpu             GPU model name, e.g. "RTX 3060"
    nvidia          True if the GPU is NVIDIA
    num_gpus        int
    vram_gb         float, per GPU
    ram_gb          float, system RAM
    dph_total       float, all-in $/hour INCLUDING the requested disk
    driver          NVIDIA driver version string, e.g. "580.126.20" (or None)
    compute_cap     int like 860 (or None)
    reliability     float 0..1 (1.0 if the provider has no such metric)
    verified        bool, provider-vetted host
    location        free text
    cpu_arch        "amd64" / "arm64"
    inet_down_mbps, inet_up_mbps     int
    down_per_tb, up_per_tb           float, $ per TB of traffic (0 if free)

Instance (dict), one rented machine.
    id              provider's instance id
    label           the label passed to create_instance (or None)
    status          one of STATUSES (normalized)
    status_detail   provider's raw status text, for logs
    started_at      epoch seconds when billing/creation started (or None)
    ssh_host, ssh_port   SSH endpoint once running (or None)
    gpu             GPU model name
    dph_total       float $/hour (or None)

The controller enforces every policy itself (price cap, VRAM, RAM, driver,
approval, runtime limit). Server-side filters in `search_offers` are only an
optimization, and the controller re-checks each returned offer.
"""

STATUSES = (
    "starting",  # provisioning, pulling the image, booting
    "running",   # container up; SSH should work
    "stopped",   # halted by the user or provider (may still bill storage)
    "failed",    # will not reach running: exited, offline, lost
)


class ProviderError(Exception):
    """A provider call failed; the message is user-facing."""


class Provider:
    name = "base"
    console_url = ""  # where the user can see/destroy instances by hand

    def __init__(self, cfg):
        self.cfg = cfg

    # -- account ---------------------------------------------------------
    def account(self):
        """-> {"id": ..., "credit": float USD available}"""
        raise NotImplementedError

    def ssh_key_registered(self, public_key_text):
        """True if this public key will be authorized on new instances."""
        raise NotImplementedError

    # -- offers ----------------------------------------------------------
    def search_offers(self, policy, disk_gb):
        """-> [Offer] priced for `disk_gb` of instance disk.

        `policy` has the controller's limits (max_dph_total, min_gpu_ram_gb,
        min_cpu_ram_gb, min_reliability, min_inet_down_mbps, ...) so the
        provider can pre-filter server-side. Order does not matter."""
        raise NotImplementedError

    # -- instances -------------------------------------------------------
    def create_instance(self, offer_id, image, disk_gb, label, env, onstart):
        """Rent `offer_id` and return the new instance id.

        env:     {name: value} environment for the container
        onstart: bash script the provider must run as root at container
                 start (it arms the runtime-limit watchdog)
        Raise ProviderError on failure. If the outcome is unknown (timeout),
        raise as well. The controller then looks for `label` via
        list_instances() and destroys any orphan."""
        raise NotImplementedError

    def list_instances(self):
        """-> [Instance] for every instance on the account."""
        raise NotImplementedError

    def get_instance(self, instance_id):
        """-> Instance or None if it no longer exists."""
        for inst in self.list_instances():
            if str(inst["id"]) == str(instance_id):
                return inst
        return None

    def ssh_endpoint(self, instance):
        """-> (host, port) for root SSH to a running instance."""
        if not (instance.get("ssh_host") and instance.get("ssh_port")):
            raise ProviderError("instance %s has no SSH endpoint yet" % instance["id"])
        return instance["ssh_host"], int(instance["ssh_port"])

    def destroy_instance(self, instance_id):
        """Request permanent deletion (ends all billing). Need not block.
        The controller confirms by polling get_instance() until it is None."""
        raise NotImplementedError

    # -- on-instance runtime limit ----------------------------------------
    def watchdog_hooks(self):
        """Bash that defines two functions for the on-instance watchdog
        (remote/onstart.sh), each returning 0 on success:

            bgpu_self_destroy   permanently delete this instance
            bgpu_self_stop      at least stop GPU billing

        It may also set BGPU_INSTANCE_ID for log lines. The default can do
        neither, so the watchdog stops the render and kills the container,
        and the Mac reaper destroys the instance."""
        return ("bgpu_self_destroy() { return 1; }\n"
                "bgpu_self_stop() { return 1; }\n")

    # -- local environment -------------------------------------------------
    def doctor_checks(self):
        """-> [(ok: bool, text, hint)] provider-specific setup checks."""
        return []

    def path_dirs(self):
        """Extra PATH directories the launchd reaper needs (e.g. a CLI)."""
        return []
