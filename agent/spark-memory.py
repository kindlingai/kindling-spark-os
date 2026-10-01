#!/usr/bin/env python3
"""Account for every byte of host memory on a GB10 node.

The GPU shares the host's memory, and its allocations do not appear in process
RSS: on one node the engine holds about 100 GiB that `ps` reports as 6 GiB
across every process on the box. `nvidia-smi --query-gpu=memory.used` answers
N/A here, but `--query-compute-apps` still gives a per-pid figure, which is
what makes the total reconcile.

Prints one table that sums to MemTotal, with the residual named rather than
hidden, so a number that moves after a config change has somewhere to land.
"""
import os
import re
import subprocess
import sys
from collections import defaultdict

GIB = 1024 ** 3


def meminfo() -> dict[str, int]:
    out = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, v = line.partition(":")
            out[k] = int(v.split()[0]) * 1024
    return out


def gpu_by_pid() -> dict[int, int]:
    """Bytes of GPU memory per pid, empty when the driver declines to say."""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,used_memory",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return {}
    out = {}
    for line in r.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            out[int(parts[0])] = int(parts[1]) * 1024 * 1024
    return out


def classify(pid: int, cmd: str) -> str:
    try:
        with open(f"/proc/{pid}/cgroup") as f:
            cg = f.read()
    except OSError:
        cg = ""
    in_docker = "docker" in cg or "containerd" in cg
    if "EngineCore" in cmd or re.search(r"\bvllm\b", cmd):
        return "vllm engine"
    if "RayWorkerProc" in cmd or "ray::" in cmd:
        return "ray workers"
    if "raylet" in cmd or "gcs_server" in cmd or "/ray/" in cmd:
        return "ray system"
    if "status-server.py" in cmd:
        return "status server"
    if in_docker:
        return "other container processes"
    if "dockerd" in cmd or "containerd" in cmd:
        return "docker daemons"
    if pid == 1 or "/systemd" in cmd or "systemd-" in cmd:
        return "systemd and units"
    if "sshd" in cmd:
        return "ssh sessions"
    return "other host processes"


def processes() -> tuple[dict[str, int], dict[str, int], dict[str, list]]:
    """(rss by role, gpu by role, notable pids by role)."""
    gpu = gpu_by_pid()
    rss_by, gpu_by = defaultdict(int), defaultdict(int)
    top = defaultdict(list)
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
            with open(f"/proc/{pid}/statm") as f:
                rss = int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError, IndexError):
            continue
        role = classify(pid, cmd)
        rss_by[role] += rss
        g = gpu.get(pid, 0)
        gpu_by[role] += g
        if g or rss > 256 * 1024 * 1024:
            top[role].append((g or rss, pid, cmd.strip()[:60], bool(g)))
    return rss_by, gpu_by, top


def main() -> int:
    m = meminfo()
    total = m["MemTotal"]
    rss_by, gpu_by, top = processes()

    gpu_total = sum(gpu_by.values())
    # Process RSS on this box excludes the GPU allocation, so the two add
    # rather than overlap. Shmem is already inside Cached.
    rss_total = sum(rss_by.values())
    cache = m["Cached"] + m["Buffers"] - m["Shmem"]
    kernel = (m["SUnreclaim"] + m["KernelStack"] + m["PageTables"]
              + m.get("VmallocUsed", 0) + m.get("Percpu", 0))
    reclaimable = m["SReclaimable"]
    free = m["MemFree"]

    rows = [("GPU allocations", gpu_total), ("process memory", rss_total),
            ("page cache", cache), ("tmpfs and shared", m["Shmem"]),
            ("kernel", kernel), ("kernel, reclaimable", reclaimable),
            ("free", free)]
    named = sum(v for _, v in rows)
    rows.append(("unaccounted", total - named))

    print(f"{'MemTotal':<28}{total / GIB:>9.2f} GiB\n")
    for label, v in rows:
        print(f"  {label:<26}{v / GIB:>9.2f} GiB{v / total:>8.1%}")

    print(f"\n{'GPU allocations by role':<28}")
    for role, v in sorted(gpu_by.items(), key=lambda kv: -kv[1]):
        if v:
            print(f"  {role:<26}{v / GIB:>9.2f} GiB")
    print(f"\n{'process memory by role':<28}")
    for role, v in sorted(rss_by.items(), key=lambda kv: -kv[1]):
        if v > 32 * 1024 * 1024:
            print(f"  {role:<26}{v / GIB:>9.2f} GiB")

    if "-v" in sys.argv:
        print("\nlargest single processes")
        flat = [x for lst in top.values() for x in lst]
        for size, pid, cmd, is_gpu in sorted(flat, reverse=True)[:12]:
            kind = "GPU" if is_gpu else "RSS"
            print(f"  {size / GIB:>7.2f} GiB {kind}  pid {pid:<8} {cmd}")

    if m.get("SwapTotal"):
        used = m["SwapTotal"] - m["SwapFree"]
        print(f"\nswap  {used / GIB:.2f} of {m['SwapTotal'] / GIB:.2f} GiB used, "
              f"{m.get('SwapCached', 0) / GIB:.2f} GiB cached")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
