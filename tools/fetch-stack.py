#!/usr/bin/env python3
"""Download one stack's .debs, check them against stacks.yaml, and index them as an apt repo.

    fetch-stack.py stacks.yaml STACK OUT_DIR

STACK may be "default". Writes OUT_DIR/*.deb, OUT_DIR/Packages and OUT_DIR/stack.env, which
holds the shell variables the rest of the build reads: STACK, KERNEL_ABI, DRIVER,
DRIVER_UPSTREAM, DRIVER_SERIES and FLAVOURS. Fails on any hash mismatch.
"""
import hashlib
import os
import subprocess
import sys
import urllib.request

import yaml


def main():
    manifest, stack_id, out = sys.argv[1:4]
    doc = yaml.safe_load(open(manifest))
    if stack_id == "default":
        stack_id = doc["default"]
    stack = doc["stacks"][stack_id]
    os.makedirs(out, exist_ok=True)

    for deb in stack["debs"]:
        path = os.path.join(out, os.path.basename(urllib.request.unquote(deb["url"])))
        if not os.path.exists(path):
            print(f"fetch {deb['name']} {deb['version']}", flush=True)
            urllib.request.urlretrieve(deb["url"], path + ".part")
            os.replace(path + ".part", path)
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        if h.hexdigest() != deb["sha256"]:
            sys.exit(f"{deb['name']}: sha256 {h.hexdigest()} does not match stacks.yaml {deb['sha256']}")

    packages = subprocess.run(["dpkg-scanpackages", "--multiversion", "."], cwd=out, check=True,
                              capture_output=True, text=True).stdout
    with open(os.path.join(out, "Packages"), "w") as f:
        f.write(packages)

    driver = stack["driver"]
    upstream = driver.split("-")[0]
    with open(os.path.join(out, "stack.env"), "w") as f:
        f.write(f"STACK={stack_id}\nKERNEL_ABI={stack['kernel_abi']}\nDRIVER={driver}\n"
                f"DRIVER_UPSTREAM={upstream}\nDRIVER_SERIES={upstream.split('.')[0]}\n"
                f"FLAVOURS=\"{' '.join(stack['flavours'])}\"\n"
                f"DISPRAM_VALIDATED={'1' if stack.get('validated', {}).get('dispram') else '0'}\n")
    print(f"stack {stack_id}: {len(stack['debs'])} debs verified")


if __name__ == "__main__":
    main()
