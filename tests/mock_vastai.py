#!/usr/bin/env python3
"""Stand-in for the `vastai` CLI used by the test-suite. No network, no money.

State lives in $MOCK_ROOT/state.json; every call is appended to calls.log.
Knobs (env): MOCK_CREDIT, MOCK_BOOT (comma list of offer ids that boot to
'exited'), MOCK_CREATE_TIMEOUT_OFFER (offer id whose create "times out" after
renting), MOCK_DESTROY_FAILS (number of initial destroy calls that are ignored).
"""
import json
import os
import subprocess
import sys
import time

ROOT = os.environ["MOCK_ROOT"]
STATE = os.path.join(ROOT, "state.json")


def load():
    try:
        return json.load(open(STATE))
    except (OSError, ValueError):
        return {"instances": [], "next_id": 9000, "destroy_calls": 0}


def save(s):
    json.dump(s, open(STATE + ".tmp", "w"), indent=1)
    os.replace(STATE + ".tmp", STATE)


def out(obj):
    # Real Vast output contains raw control characters inside strings.
    print(json.dumps(obj).replace("<TAB>", "\t").replace("<CR>", "\r"))
    sys.exit(0)


def opt(args, name, default=None):
    return args[args.index(name) + 1] if name in args else default


OFFERS = [
    # good, cheapest RTX
    dict(id=101, machine_id=1, gpu_name="RTX 3060", gpu_ram=12288, cpu_ram=32000, dph_base=0.060, storage_cost=0.2,
         driver_version="580.126.20", compute_cap=860, reliability2=0.99, verified=True, geolocation="Norway, NO",
         inet_down=800, inet_up=500, internet_down_cost_per_tb=2.0, internet_up_cost_per_tb=2.0, cpu_arch="amd64"),
    # good, second
    dict(id=102, machine_id=2, gpu_name="RTX 3060", gpu_ram=12288, cpu_ram=24000, dph_base=0.070, storage_cost=0.2,
         driver_version="595.71.05", compute_cap=860, reliability2=0.99, verified=True, geolocation="Michigan, US",
         inet_down=600, inet_up=500, internet_down_cost_per_tb=2.0, internet_up_cost_per_tb=2.0, cpu_arch="amd64"),
    # cheap but only within cap at 5 GB disk (must be rejected client-side)
    dict(id=103, machine_id=3, gpu_name="RTX 3060", gpu_ram=12288, cpu_ram=32000, dph_base=0.097, storage_cost=0.4,
         driver_version="580.1", compute_cap=860, reliability2=0.99, verified=True, geolocation="X",
         inet_down=800, inet_up=500, internet_down_cost_per_tb=2.0, internet_up_cost_per_tb=2.0, cpu_arch="amd64"),
    # driver too old for OptiX in Blender 5.2
    dict(id=104, machine_id=4, gpu_name="Tesla P100", gpu_ram=16384, cpu_ram=32000, dph_base=0.050, storage_cost=0.2,
         driver_version="535.230.02", compute_cap=600, reliability2=0.99, verified=True, geolocation="Y",
         inet_down=800, inet_up=500, internet_down_cost_per_tb=2.0, internet_up_cost_per_tb=2.0, cpu_arch="amd64"),
    # V100: allowed (OptiX works without RT cores) but ranked after RTX
    dict(id=105, machine_id=5, gpu_name="Tesla V100", gpu_ram=16384, cpu_ram=32000, dph_base=0.055, storage_cost=0.2,
         driver_version="580.178.04", compute_cap=700, reliability2=0.99, verified=True, geolocation="Z",
         inet_down=800, inet_up=500, internet_down_cost_per_tb=2.0, internet_up_cost_per_tb=2.0, cpu_arch="amd64"),
    # over the hard price cap
    dict(id=106, machine_id=6, gpu_name="RTX 4090", gpu_ram=24576, cpu_ram=64000, dph_base=0.35, storage_cost=0.2,
         driver_version="580.1", compute_cap=890, reliability2=0.99, verified=True, geolocation="W",
         inet_down=800, inet_up=500, internet_down_cost_per_tb=2.0, internet_up_cost_per_tb=2.0, cpu_arch="amd64"),
]


def main():
    args = [a for a in sys.argv[1:] if a != "--raw"]
    with open(os.path.join(ROOT, "calls.log"), "a") as f:
        f.write(json.dumps(args) + "\n")
    s = load()
    cmd = " ".join(args[:2])

    if cmd == "show user":
        out({"id": 1, "credit": float(os.environ.get("MOCK_CREDIT", "5.0")), "balance": 0})
    if cmd == "show ssh-keys":
        out([{"id": 1, "public_key": open(os.environ["MOCK_PUBKEY"]).read().strip()}])
    if cmd == "search offers":
        disk = float(opt(args, "--storage", 5))
        res = []
        for o in OFFERS:
            o = dict(o)
            o["dph_total"] = o["dph_base"] + o["storage_cost"] * disk / 720.0
            if o["dph_base"] + o["storage_cost"] * 5 / 720.0 <= 0.10:  # server-side filter quirk
                res.append(o)
        out(res)
    if cmd == "create instance":
        oid = int(args[2])
        iid = s["next_id"]
        s["next_id"] += 1
        inst = {"id": iid, "label": opt(args, "--label"), "actual_status": "loading", "intended_status": "running",
                "start_date": time.time(), "ssh_host": "127.0.0.1", "ssh_port": iid, "gpu_name": "RTX 3060",
                "dph_total": 0.07, "offer": oid, "polls": 0, "status_msg": "#6 5.7 Fetched 51 MB<TAB>(9325 kB/s)<CR>", "image": opt(args, "--image"), "env": opt(args, "--env")}
        s["instances"].append(inst)
        save(s)
        rroot = os.path.join(ROOT, "remote", str(iid))
        os.makedirs(rroot, exist_ok=True)
        onstart = opt(args, "--onstart-cmd")
        if onstart:
            with open(os.path.join(rroot, "..", "%d.onstart.sh" % iid), "w") as f:
                f.write(onstart)
            env = dict(os.environ, BGPU_REMOTE_ROOT=rroot, CONTAINER_ID=str(iid), CONTAINER_API_KEY="mock",
                       BGPU_API_BASE="http://127.0.0.1:9/never")
            subprocess.run(["bash", "-c", onstart], env=env, timeout=30)
        if str(oid) == os.environ.get("MOCK_CREATE_TIMEOUT_OFFER"):
            print("Error: request timed out", file=sys.stderr)
            sys.exit(1)
        out({"success": True, "new_contract": iid})
    if cmd in ("show instances", "show instance"):
        for i in s["instances"]:
            i["polls"] = i.get("polls", 0) + 1
            if i.get("actual_status") == "loading" and i["polls"] >= 2:
                bad = os.environ.get("MOCK_BOOT", "").split(",")
                i["actual_status"] = "exited" if str(i.get("offer")) in bad else "running"
        save(s)
        if cmd == "show instance":
            out([i for i in s["instances"] if str(i["id"]) == args[2]][0])
        out(s["instances"])
    if cmd == "ssh-url":
        out("ssh://root@127.0.0.1:%s" % args[2])
    if cmd == "destroy instance":
        s["destroy_calls"] = s.get("destroy_calls", 0) + 1
        if s["destroy_calls"] <= int(os.environ.get("MOCK_DESTROY_FAILS", "0")):
            save(s)
            out({"success": True})  # silently ignored, like a flaky API
        s["instances"] = [i for i in s["instances"] if str(i["id"]) != args[2]]
        s.setdefault("destroyed", []).append(int(args[2]))
        save(s)
        out({"success": True})
    print("mock_vastai: unsupported command %r" % args, file=sys.stderr)
    sys.exit(2)


main()
