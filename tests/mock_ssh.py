#!/usr/bin/env python3
"""Stand-in for `ssh` used by the test-suite. Runs the "remote" command locally
with bash, inside $MOCK_ROOT/remote/<instance-id> (the port encodes the id).

MOCK_CORRUPT_DOWNLOAD=N corrupts the first N output downloads (flips a byte in
the tar stream) to exercise checksum verification.
"""
import os
import subprocess
import sys

ROOT = os.environ["MOCK_ROOT"]
argv = sys.argv[1:]
port = argv[argv.index("-p") + 1]
i = 0
while i < len(argv):  # skip options to find user@host
    if argv[i] in ("-p", "-i", "-o"):
        i += 2
    elif argv[i].startswith("-"):
        i += 1
    else:
        break
command = " ".join(argv[i + 1:])
rroot = os.path.join(ROOT, "remote", port)
if not os.path.isdir(rroot):
    sys.stderr.write("ssh: connect to host 127.0.0.1 port %s: Connection refused\n" % port)
    sys.exit(255)
command = command.replace("/workspace/bgpu", rroot)
env = dict(os.environ, BGPU_REMOTE_ROOT=rroot, BGPU_BLENDER_BIN=os.environ["MOCK_BLENDER"],
           COPYFILE_DISABLE="1")  # behave like Linux tar: no AppleDouble ._ entries

is_output_download = "tar -cf - -C " in command and command.rstrip().endswith("/job/output .")
counter = os.path.join(ROOT, "corrupt_count")
n = int(open(counter).read()) if os.path.exists(counter) else 0
if is_output_download and n < int(os.environ.get("MOCK_CORRUPT_DOWNLOAD", "0")):
    open(counter, "w").write(str(n + 1))
    import io
    import tarfile
    data = subprocess.run(["bash", "-c", command], env=env, capture_output=True).stdout
    src, dst = tarfile.open(fileobj=io.BytesIO(data)), io.BytesIO()
    with tarfile.open(fileobj=dst, mode="w") as t:
        flipped = False
        for m in src.getmembers():
            if m.isfile() and not flipped:
                body = bytearray(src.extractfile(m).read())
                body[len(body) // 2] ^= 0xFF  # silent bit-rot inside the image data
                flipped = True
                t.addfile(m, io.BytesIO(bytes(body)))
            else:
                t.addfile(m, src.extractfile(m) if m.isfile() else None)
    sys.stdout.buffer.write(dst.getvalue())
    sys.exit(0)
if "cat >" in command:  # small text payloads (spec.json) carry remote paths too
    payload = sys.stdin.buffer.read().replace(b"/workspace/bgpu", rroot.encode())
    sys.exit(subprocess.run(["bash", "-c", command], env=env, input=payload).returncode)
sys.exit(subprocess.call(["bash", "-c", command], env=env))
