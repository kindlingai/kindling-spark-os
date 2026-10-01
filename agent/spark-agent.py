#!/usr/bin/env python3
"""Host agent: general MCP tools for one node, served through mentat.

Runs beside the model containers rather than inside one, so the capabilities
that need a host (reading every process, ptrace, kernel messages) leave the
process that runs the model.

Tools return what the underlying command printed. A parsed view discards the
fields nobody thought to keep, and on 2026-08-25 the answer to where 100 GiB
of memory went was in `nvidia-smi --query-compute-apps`, which the curated GPU
tool did not carry. Correlating several sources is a separate tool, named for
the question it answers.

The agent registers its MCP endpoint with the local mentatd, and mentat-serve
merges it with every other node's, so clients reach every node's tools
through the router. Stdlib only, apart from the mentat shim's ray.register,
and it runs without that. Prototype on a spare port with:
    python3 spark-agent.py --port 8090
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Server(ThreadingHTTPServer):
    """The HTTP listener.

    socketserver defaults `request_queue_size` to 5, and that is the listen()
    backlog. Past five connections the kernel has not yet handed over, it
    refuses new ones, and the refusal reaches the client as a transport error
    naming nothing -- it read as model failures through an afternoon of
    benchmarking before the listener was suspected. It must be a class
    attribute: TCPServer.__init__ calls listen() before the instance exists to
    set it on. The kernel clamps to net.core.somaxconn.
    """

    request_queue_size = 1024
    daemon_threads = True

HOSTNAME = socket.gethostname()
STARTED = time.time()
LOG_DIR = os.environ.get("AGENT_LOG_DIR", "/logs")
AGENT_PORT = int(os.environ.get("AGENT_PORT", "8090"))
# Set from --port in main(); the page links peers on whatever port this
# agent is actually served on rather than assuming the default.
LISTEN_PORT = AGENT_PORT
MAX_BYTES = 64 * 1024

# mentat owns the model list: containers register with mentatd, and the
# mentat-serve router decides what is routable. The agent reads the router's
# table rather than keeping a registry of its own, so the two cannot disagree.
MENTAT_ROUTER_URL = os.environ.get("MENTAT_ROUTER_URL", "")
# How often the router and the engines are read. Token rates are differenced
# across this interval, so it also sets how twitchy the numbers look.
LOAD_INTERVAL_S = float(os.environ.get("LOAD_INTERVAL_S", "5"))


def run(cmd: list[str], timeout: float = 15) -> str:
    """Combined stdout and stderr, or a line saying why there is none."""
    if not shutil.which(cmd[0]):
        return f"({cmd[0]} is not installed on {HOSTNAME})"
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"({' '.join(cmd)} timed out after {timeout}s)"
    except OSError as e:
        return f"({' '.join(cmd)} failed: {type(e).__name__}: {e})"
    return (r.stdout or "") + (r.stderr or "")


def read_file(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except OSError as e:
        return f"({path}: {type(e).__name__}: {e})"


def cap(text: str, limit: int = MAX_BYTES) -> str:
    b = text.encode("utf-8", "replace")
    if len(b) <= limit:
        return text
    head = b[:limit].decode("utf-8", "ignore")
    return f"{head}\n\n[truncated at {limit} bytes of {len(b)}]"


def stamped(body: str, source: str) -> str:
    """Every answer says where it came from and when."""
    return (f"[{HOSTNAME} {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] "
            f"{source}\n{body}")


# --- raw tools -------------------------------------------------------------
# One command in, its output out. Nothing between.

def t_meminfo(**_):
    return stamped(read_file("/proc/meminfo"), "cat /proc/meminfo")


def t_gpu(args: str = "", **_):
    """nvidia-smi with the caller's own flags, from a fixed set.

    --query-gpu reports N/A for memory on GB10 while --query-compute-apps
    still gives a per-pid figure, so the caller needs to pick.
    """
    allowed = {
        "": ["nvidia-smi"],
        "query": ["nvidia-smi", "--query-gpu=name,utilization.gpu,clocks.sm,"
                  "temperature.gpu,power.draw,memory.total,memory.used",
                  "--format=csv"],
        "apps": ["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
                 "--format=csv"],
        "topo": ["nvidia-smi", "topo", "-m"],
        "clocks": ["nvidia-smi", "-q", "-d", "CLOCK,PERFORMANCE"],
    }
    if args not in allowed:
        return f"unknown selection {args!r}. Available: {', '.join(sorted(allowed))}"
    return stamped(cap(run(allowed[args])), " ".join(allowed[args]))


def t_processes(**_):
    return stamped(cap(run(["ps", "-eo",
                            "pid,ppid,rss,pcpu,etime,nlwp,comm,args", "--sort=-rss"])),
                   "ps -eo pid,ppid,rss,pcpu,etime,nlwp,comm,args --sort=-rss")


def t_network(**_):
    parts = [("ip -o -4 addr", run(["ip", "-o", "-4", "addr"])),
             ("ip -o link", run(["ip", "-o", "link"])),
             ("rdma link show", run(["rdma", "link", "show"]))]
    return stamped("\n".join(f"$ {c}\n{o}" for c, o in parts), "network")


def t_rdma_counters(device: str = "", **_):
    base = "/sys/class/infiniband"
    out = []
    try:
        devs = [device] if device else sorted(os.listdir(base))
    except OSError as e:
        return stamped(f"({base}: {e})", "rdma counters")
    for d in devs:
        for group in ("counters", "hw_counters"):
            p = f"{base}/{d}/ports/1/{group}"
            try:
                names = sorted(os.listdir(p))
            except OSError:
                continue
            out.append(f"# {d}/{group}")
            for n in names:
                out.append(f"{n} = {read_file(f'{p}/{n}').strip()}")
    return stamped(cap("\n".join(out) or "(no infiniband devices)"), "rdma counters")


def t_pci(**_):
    return stamped(cap(run(["lspci", "-nnk"])), "lspci -nnk")


def t_usb(**_):
    return stamped(cap(run(["lsusb", "-t"])), "lsusb -t")


def t_docker_ps(**_):
    """Container inventory, from the host's snapshot.

    Reading it from a file keeps the Docker socket out of this process. The
    socket is root-equivalent on the host, and this endpoint answers a model.
    """
    p = os.path.join(LOG_DIR, "docker-ps.log")
    try:
        age = time.time() - os.stat(p).st_mtime
    except OSError as e:
        return stamped(f"({p}: {type(e).__name__}: {e})", "docker ps snapshot")
    return stamped(cap(read_file(p)), f"docker-ps.log, {age:.0f}s old")


def t_systemd(**_):
    """Running services, from the host's snapshot.

    Same reason as t_docker_ps: this image has no systemctl, and the way to
    give it one is to mount the host's /run/systemd, which grants control and
    not only a read. So the host writes the list and this reads a file.
    """
    p = os.path.join(LOG_DIR, "systemd-units.log")
    try:
        age = time.time() - os.stat(p).st_mtime
    except OSError as e:
        return stamped(f"({p}: {type(e).__name__}: {e})", "systemd snapshot")
    return stamped(cap(read_file(p)), f"systemd-units.log, {age:.0f}s old")


def t_list_logs(**_):
    try:
        names = sorted(os.listdir(LOG_DIR))
    except OSError as e:
        return stamped(f"({LOG_DIR}: {e})", "list logs")
    rows = []
    for n in names:
        p = os.path.join(LOG_DIR, n)
        try:
            st = os.stat(p)
        except OSError:
            continue
        rows.append(f"{st.st_size:>12}  {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(st.st_mtime))}  {n}")
    return stamped("\n".join(rows) or "(empty)", f"ls -l {LOG_DIR}")


def t_read_log(file: str = "", lines: int = 200, grep: str = "", **_):
    """Tail of one file under the log directory, optionally filtered."""
    if not file:
        return "Name a file. Use list_logs to see them."
    root = os.path.realpath(LOG_DIR)
    target = os.path.realpath(os.path.join(root, file))
    if target != root and not target.startswith(root + os.sep):
        return "That path leaves the log directory."
    body = read_file(target)
    out = body.splitlines()
    if grep:
        try:
            import re
            rx = re.compile(grep)
            out = [l for l in out if rx.search(l)]
        except re.error as e:
            return f"(bad pattern: {e})"
    tail = out[-max(1, min(int(lines or 200), 5000)):]
    return stamped(cap("\n".join(tail)), f"{file} ({len(out)} matching lines)")


def _engine_pids() -> list[tuple[int, str]]:
    """(host pid, label) for the engine processes, engine core first.

    These are container processes seen through the host's /proc, which is why
    the agent can dump them without the container holding CAP_SYS_PTRACE.
    """
    out = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return out
    for d in entries:
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if "EngineCore" in cmd:
            out.insert(0, (int(d), "EngineCore"))
        elif "RayWorkerProc" in cmd:
            out.append((int(d), "RayWorkerProc"))
        elif "/bin/vllm serve" in cmd:
            out.append((int(d), "APIServer"))
    return out[:4]


PY_SPY = os.environ.get("PY_SPY", "py-spy")


def t_engine_stacks(rounds: int = 3, gap_s: float = 5, **_):
    """Python stacks of the engine processes, sampled several times.

    One sample cannot tell a slow kernel from queue backpressure. CUDA
    launches are async, so when the GPU stream backs up the launch queue fills
    and whatever the CPU tries to launch next is what blocks. A frame that
    stays put across rounds is the kernel actually running. A frame that moves
    means the cause is upstream of where the sample landed.
    """
    rounds = max(1, min(int(rounds or 3), 10))
    gap_s = max(0.5, min(float(gap_s or 5), 30))
    pids = _engine_pids()
    if not pids:
        return stamped("(no engine processes found)", "engine stacks")
    out = []
    for i in range(rounds):
        if i:
            time.sleep(gap_s)
        out.append(f"----- round {i+1}/{rounds} at "
                   f"{time.strftime('%H:%M:%SZ', time.gmtime())} -----")
        for pid, label in pids:
            if label == "APIServer" and i:
                continue          # waits on the engine by construction
            out.append(f"===== {label} (host pid {pid}) =====")
            out.append(run([PY_SPY, "dump", "--pid", str(pid)], timeout=30))
    return stamped(cap("\n".join(out)), f"{PY_SPY} dump x{rounds}")


# --- tools that correlate ---------------------------------------------------

def t_memory_accounting(**_):
    """Every byte of host memory, by owner.

    Separate from meminfo because it needs three sources: /proc/meminfo,
    per-process RSS, and the GPU's per-pid figure. GPU allocations skip
    process RSS entirely, so ps accounts for about 6 GiB of 121 on a serving
    node and the total only closes once the GPU number is added.
    """
    helper = os.environ.get("SPARK_MEMORY_PY", "/usr/local/bin/spark-memory.py")
    if os.path.exists(helper):
        return stamped(cap(run(["python3", helper, "-v"], timeout=30)), helper)
    return stamped(f"({helper} not present)", "memory accounting")


def t_models(**_):
    """The router's view of every model, with the load this agent scraped."""
    cluster = router_snapshot()
    with _load_lock:
        loads = dict(_load)
    return json.dumps({"router": MENTAT_ROUTER_URL, **cluster,
                       "load": loads}, indent=2)



# --- load sampling ----------------------------------------------------------
# The agent scrapes each engine the router lists rather than the engine
# reporting its load. That keeps every model image out of this: a container
# announces where it serves to mentat, and what it is doing is derived here.

_load: dict[str, dict] = {}
_load_lock = threading.Lock()
_load_prev: dict[str, tuple[float, float, float]] = {}

# The names are exact and were read off a running engine. kv_cache_usage_perc
# is NOT gpu_cache_usage_perc, which is what the older status page scraped and
# which silently reports nothing on this vLLM.
_GAUGES = {
    "vllm:num_requests_running": "running",
    "vllm:num_requests_waiting": "waiting",
    "vllm:kv_cache_usage_perc": "kv",
}
_COUNTERS = {
    "vllm:prompt_tokens_total": "prompt",
    "vllm:generation_tokens_total": "gen",
}


def _parse_metrics(text: str) -> dict:
    """Pull the handful of series worth showing out of Prometheus text.

    Labels are ignored beyond stripping them: one engine per container here,
    so a bare sum over duplicate series is the right reading.
    """
    out: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line[0] == "#":
            continue
        name, _, rest = line.partition("{")
        if rest:
            _, _, value = rest.partition("}")
        else:
            name, _, value = line.partition(" ")
        key = _GAUGES.get(name.strip()) or _COUNTERS.get(name.strip())
        if not key:
            continue
        try:
            out[key] = out.get(key, 0.0) + float(value.strip())
        except ValueError:
            continue
    return out




def read_interconnect() -> dict:
    """This node's RoCE rates, from the host snapshot.

    The snapshot samples two counter reads a second apart, which is the only
    way to say whether the link is busy now rather than how much it has ever
    carried. Absent on a node with no cable plugged in, which is not an error.
    """
    p = os.path.join(LOG_DIR, "interconnect.log")
    try:
        age = time.time() - os.stat(p).st_mtime
        text = read_file(p)
    except OSError:
        return {}
    if age > 300:                       # a snapshot this old describes nothing
        return {}
    # The snapshot writes: "<dev> tx <n> MB/s  rx <n> MB/s", so the units are
    # fields of their own and the rx label lands at index 4, not 3.
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 7 and parts[1] == "tx" and parts[4] == "rx":
            try:
                return {"dev": parts[0], "tx_mbs": float(parts[2]),
                        "rx_mbs": float(parts[5]), "age_s": round(age)}
            except ValueError:
                continue
    return {}


# How long a driver fault stays interesting. Anything older is history: these
# lines are never cleared, so without a window a single fault at boot would
# read as a permanently broken node.
DRIVER_ERR_WINDOW_S = 900


def read_driver_errors() -> dict:
    """NVRM and Xid lines from the host snapshot, counted and dated.

    Returns {} when the snapshot is missing, stale or clean. `recent` counts
    the window; `total` is the whole boot, which can be far larger.

    This exists because nothing watched it. On 2026-08-27 the driver logged
    NV_ERR_NO_MEMORY every few minutes for 23 minutes while the model answered
    requests at normal speed, and the first visible symptom was a box that had
    to be power cycled.
    """
    p = os.path.join(LOG_DIR, "driver-errors.log")
    try:
        age = time.time() - os.stat(p).st_mtime
        text = read_file(p)
    except OSError:
        return {}
    if age > 300:
        return {}
    total, recent, newest, sample = 0, 0, None, ""
    now = time.time()
    for line in text.splitlines():
        if line.startswith("# matched "):
            try:
                total = int(line.split()[2])
            except (IndexError, ValueError):
                pass
            continue
        if not line.startswith("["):
            continue
        stamp, _, msg = line[1:].partition("] ")
        try:
            # dmesg -T renders "Thu Aug 27 22:13:26 2026" in local time.
            when = time.mktime(time.strptime(stamp, "%a %b %d %H:%M:%S %Y"))
        except ValueError:
            continue
        if newest is None or when > newest:
            newest, sample = when, msg.strip()[:200]
        if now - when <= DRIVER_ERR_WINDOW_S:
            recent += 1
    if not total and newest is None:
        return {}
    return {"total": total, "recent": recent, "sample": sample,
            "last_s": round(now - newest) if newest else None}




# --- mentat ------------------------------------------------------------------
# mentat does the cluster work this agent used to do itself. mentatd knows the
# nodes, and mentat-serve routes the models and merges every group's MCP. The
# agent registers its own MCP endpoint as a group of one named for the node, so
# its host tools reach clients through the router, where `__group` picks the
# node.
MENTAT_GROUP = os.environ.get("MENTAT_GROUP", f"agent-{HOSTNAME}")
MENTAT_DAEMON = os.environ.get("MENTAT_DAEMON", "127.0.0.1:6379")
# Short, because one loop reads the router, every peer agent and every engine
# in turn, and a dead node should cost a round seconds, not minutes.
FETCH_TIMEOUT_S = 2.0


def register_loop():
    """Hold this agent's registration with the local daemon.

    ray.register's main() refuses a registration with no OpenAI endpoint,
    because one usually means a model that never came up. This agent has no
    model on purpose, so it calls connect() and keeps main()'s retry shape.
    """
    try:
        from ray import register
    except ImportError as e:
        print(f"mentat registration off: {e}", flush=True)
        return
    args = register.parse_args([
        "--address", MENTAT_DAEMON, "--group", MENTAT_GROUP,
        "--mcp", f"{LISTEN_PORT}/mcp", "--container", "spark-agent"])
    delay = 1.0
    while True:
        try:
            if register.connect(args) > register.SETTLED_S:
                delay = 1.0
        except (OSError, RuntimeError, ValueError) as e:
            print(f"mentat registration: {type(e).__name__}: {e}", flush=True)
        time.sleep(delay)
        delay = min(delay * 2, register.RETRY_MAX_S)


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_S) as r:
        return json.loads(r.read().decode())


# The last complete read of the cluster: the router's table and what each
# node's agent reported about itself.
_cluster: dict = {"router": None, "router_error": "not read yet", "nodes": {}}
_cluster_lock = threading.Lock()


def router_snapshot() -> dict:
    with _cluster_lock:
        return dict(_cluster)


def read_nodes(daemons: dict) -> dict[str, dict]:
    """daemon address -> that node's agent status, or why there is none.

    The router's watch set is the node list. mentat knows nothing of the
    interlink or the driver, so each node's agent is asked for its own.
    """
    out = {}
    for addr, d in sorted(daemons.items()):
        ip = addr.rsplit(":", 1)[0]
        entry = {"ip": ip, "addrs": [ip] + [a.rsplit(":", 1)[0]
                                            for a in d.get("alternates") or []],
                 "connected": bool(d.get("connected")),
                 "daemon_error": d.get("error")}
        try:
            entry["agent"] = _get_json(f"http://{ip}:{LISTEN_PORT}/status.json")
        except Exception as e:
            entry["agent_error"] = f"{type(e).__name__}: {e}"
        out[addr] = entry
    return out


def watch_cluster():
    """Read the router and every node's agent, then scrape each engine.

    Token rates come from differencing the totals, because vLLM publishes
    counters and a counter says nothing about now.
    """
    while True:
        router, err = None, "MENTAT_ROUTER_URL is not set"
        if MENTAT_ROUTER_URL:
            try:
                router, err = _get_json(MENTAT_ROUTER_URL.rstrip("/") + "/"), None
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
        nodes = read_nodes((router or {}).get("daemons") or {})
        with _cluster_lock:
            _cluster.update(router=router, router_error=err, nodes=nodes)
        groups = (router or {}).get("groups") or {}
        for name, g in groups.items():
            if g.get("openai"):
                sample_load(name, g["openai"])
        with _load_lock:
            for gone in set(_load) - set(groups):
                _load.pop(gone, None)
        time.sleep(LOAD_INTERVAL_S)


def sample_load(name: str, url: str):
    parts = urllib.parse.urlsplit(url)
    try:
        with urllib.request.urlopen(
                f"{parts.scheme}://{parts.netloc}/metrics",
                timeout=FETCH_TIMEOUT_S) as r:
            m = _parse_metrics(r.read().decode("utf-8", "replace"))
    except Exception:
        with _load_lock:
            _load.pop(name, None)
        _load_prev.pop(name, None)
        return
    now = time.time()
    entry = {"running": int(m.get("running", 0)),
             "waiting": int(m.get("waiting", 0)),
             "kv": round(m.get("kv", 0.0) * 100, 1)}
    prev = _load_prev.get(name)
    if prev:
        dt = now - prev[0]
        # A restarted engine resets its counters, so a negative delta is a
        # new engine rather than a rate. Drop it and re-baseline.
        if dt >= 1.0:
            dp, dg = m.get("prompt", 0.0) - prev[1], m.get("gen", 0.0) - prev[2]
            if dp >= 0 and dg >= 0:
                entry["prompt_tps"] = round(dp / dt, 1)
                entry["gen_tps"] = round(dg / dt, 1)
    _load_prev[name] = (now, m.get("prompt", 0.0), m.get("gen", 0.0))
    with _load_lock:
        _load[name] = entry


TOOLS = {
    "meminfo": (t_meminfo, "Raw /proc/meminfo.", {}),
    "gpu": (t_gpu, "nvidia-smi output. Pick a view with args: '' for the "
            "default table, 'query' for a csv of utilisation/clocks/temp/power, "
            "'apps' for per-pid GPU memory, 'topo' for the topology matrix, "
            "'clocks' for clock and performance detail. On GB10 --query-gpu "
            "reports N/A for memory while 'apps' still answers.",
            {"args": {"type": "string"}}),
    "processes": (t_processes, "Raw ps, sorted by RSS. GPU allocations do not "
                  "appear here.", {}),
    "network": (t_network, "Raw ip addr, ip link and rdma link output.", {}),
    "rdma_counters": (t_rdma_counters, "Every RoCE counter for a port, raw. "
                      "Rates need two reads: these are lifetime totals.",
                      {"device": {"type": "string"}}),
    "pci": (t_pci, "Raw lspci -nnk, including the bound driver.", {}),
    "usb": (t_usb, "Raw lsusb -t.", {}),
    "docker_ps": (t_docker_ps, "Raw docker ps -a.", {}),
    "systemd_units": (t_systemd, "Running systemd services.", {}),
    "list_logs": (t_list_logs, "Files in the log directory with size and mtime.", {}),
    "read_log": (t_read_log, "Tail of one log file, optionally filtered by a "
                 "regex. Capped at 64 KB.",
                 {"file": {"type": "string"}, "lines": {"type": "integer"},
                  "grep": {"type": "string"}}),
    "memory_accounting": (t_memory_accounting, "Host memory by owner, "
                          "reconciled against MemTotal. Needs /proc/meminfo, "
                          "per-process RSS and the GPU's per-pid figure "
                          "together, which is why it is not raw meminfo.", {}),
    "engine_stacks": (t_engine_stacks, "Python stacks of the engine processes, "
                      "sampled over several rounds. A frame that stays put is "
                      "the kernel running; one that moves is queue "
                      "backpressure, since an async CUDA launch blocks on "
                      "whatever it tries to launch next. Reads container "
                      "processes through the host's /proc.",
                      {"rounds": {"type": "integer"},
                       "gap_s": {"type": "number"}}),
    "models": (t_models, "mentat-serve's table of every group and model, "
               "with the token load this agent scraped from each engine.", {}),
}




# --- cluster page -----------------------------------------------------------
# Every agent reads the same router, so any node can draw the cluster and
# there is no page that only one node can serve.
_CSS = """body{font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;
max-width:60rem;margin:2rem auto;padding:0 1rem;background:#111;color:#ddd}
h1{font-size:1.2rem}h2{font-size:1rem;margin-top:2rem;color:#9ad}
table{border-collapse:collapse;width:100%;margin:.5rem 0}
th,td{text-align:left;padding:.3rem .6rem;border-bottom:1px solid #333}
th{color:#888;font-weight:normal}.ok{color:#7c7}.bad{color:#e77}.dim{color:#777}
.self{color:#fc6}"""
_DIM_DASH = "<span class=dim>-</span>"


def _esc(v) -> str:
    return (str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _driver_cell(dv: dict) -> str:
    # A fault inside the window is the alarm. Older ones stay visible but dim,
    # because the count never resets short of a reboot.
    if dv.get("recent"):
        return (f"<span class=bad title=\"{_esc(dv.get('sample') or '')}\">"
                f"{dv['recent']} in {DRIVER_ERR_WINDOW_S // 60}m</span>")
    if dv.get("total"):
        return f"<span class=dim>{dv['total']} earlier</span>"
    return _DIM_DASH


def cluster_page() -> bytes:
    snap = router_snapshot()
    router = snap.get("router") or {}
    groups = router.get("groups") or {}
    with _load_lock:
        loads = dict(_load)

    # An engine URL names an address. Map each address back to the node that
    # holds it, so the models table can say where a model runs.
    by_ip: dict[str, str] = {}
    for n in snap.get("nodes", {}).values():
        name = (n.get("agent") or {}).get("node") or n["ip"]
        for a in n["addrs"]:
            by_ip[a] = name

    def node_of(url: str | None) -> str:
        host = urllib.parse.urlsplit(url or "").hostname or ""
        return by_ip.get(host, host or "?")

    served = {}
    for g in groups.values():
        if g.get("openai"):
            n = node_of(g["openai"])
            served[n] = served.get(n, 0) + 1

    rows = []
    for n in snap.get("nodes", {}).values():
        agent = n.get("agent") or {}
        name = agent.get("node") or n["ip"]
        link = (f"<a href=\"http://{_esc(n['ip'])}:{LISTEN_PORT}/\" "
                f"style=\"color:#9ad\">{_esc(name)}</a>")
        if name == HOSTNAME:
            link += " <span class=self>this node</span>"
        mentat = ("<span class=ok>connected</span>" if n["connected"] else
                  f"<span class=bad>{_esc(n.get('daemon_error') or 'down')}</span>")
        if "agent" in n:
            ic = agent.get("interconnect") or {}
            ics = (f"{ic['tx_mbs']:.0f}/{ic['rx_mbs']:.0f} MB/s"
                   if ic else _DIM_DASH)
            drv = _driver_cell(agent.get("driver") or {})
        else:
            ics = f"<span class=bad title=\"{_esc(n.get('agent_error'))}\">no agent</span>"
            drv = _DIM_DASH
        rows.append(
            f"<tr><td>{link}</td><td>{_esc(', '.join(n['addrs']))}</td>"
            f"<td>{mentat}</td><td>{ics}</td><td>{drv}</td>"
            f"<td>{served.get(name, 0)}</td></tr>")

    models = []
    for name, g in sorted(groups.items()):
        if not g.get("openai"):
            continue                    # agents and other MCP-only groups
        healthy = bool(g.get("healthy"))
        ld = loads.get(name) or {}
        if "gen_tps" in ld:
            # Prompt and generation are separate on purpose: a long prefill
            # shows as prompt tokens with generation at zero, which is the
            # difference between a busy engine and a stalled one.
            tps = f"{ld['prompt_tps']:.0f} in / {ld['gen_tps']:.0f} out"
        elif ld:
            tps = "<span class=dim>warming</span>"
        else:
            tps = _DIM_DASH
        q = f"{ld['running']}/{ld['waiting']}" if ld else _DIM_DASH
        kv = f"{ld['kv']:.0f}%" if ld else _DIM_DASH
        models.append(
            f"<tr><td>{_esc(', '.join(g.get('models') or [name]))}</td>"
            f"<td>{_esc(name)}</td><td>{_esc(node_of(g['openai']))}</td>"
            f"<td class={'ok' if healthy else 'bad'}>"
            f"{'healthy' if healthy else _esc(g.get('why_not') or 'not ready')}</td>"
            f"<td>{tps}</td><td>{q}</td><td>{kv}</td>"
            f"<td class=dim>{_esc(g['openai'])}</td></tr>")

    err = snap.get("router_error")
    router_note = (f"<p class=bad>router unreachable: {_esc(err)}</p>" if err else "")
    empty = "router unreachable" if err else "none routed"
    html = f"""<!doctype html><meta charset=utf-8>
<meta http-equiv=refresh content=10>
<title>spark cluster</title><style>{_CSS}</style>
<h1>spark cluster &middot; seen from {_esc(HOSTNAME)}</h1>
{router_note}
<h2>nodes</h2>
<table><tr><th>node</th><th>addresses</th><th>mentatd</th>
<th>interlink tx/rx</th><th>driver faults</th><th>models</th></tr>
{''.join(rows) or '<tr><td colspan=6 class=dim>no daemons known</td></tr>'}</table>
<h2>models</h2>
<table><tr><th>model</th><th>group</th><th>node</th><th>state</th>
<th>tokens/s</th><th>run/wait</th><th>kv</th>
<th>serving</th></tr>{''.join(models) or f'<tr><td colspan=8 class=dim>{empty}</td></tr>'}</table>
<p class=dim>from <a href="{_esc(MENTAT_ROUTER_URL)}/" style="color:#9ad">{_esc(MENTAT_ROUTER_URL)}</a>
&middot; refreshes every 10s
&middot; <a href="/status.json" style="color:#9ad">status.json</a></p>"""
    return html.encode()


# --- who may ask ------------------------------------------------------------
# The network is the only control: the endpoint answers our own subnets and
# the router. Docker bridge ranges are absent on purpose, so a container on
# this host cannot read host state just by being here. A bridge-networked
# client still reaches the agent when it dials a real host address, because
# Docker masquerades the source to the host's own IP on that route.
# Address prefixes, not CIDR blocks. Loopback alone by default: the compose
# file takes the real subnets from the node's .env.
ALLOWED_SOURCES = [p.strip() for p in os.environ.get(
    "ALLOWED_SOURCES", "127.0.0.1").split(",") if p.strip()]
# The router forwards every merged tool call, so its address is always let in.
ROUTER_HOST = urllib.parse.urlsplit(MENTAT_ROUTER_URL).hostname or ""


def source_allowed(addr: str) -> bool:
    return addr == ROUTER_HOST or any(addr.startswith(p) for p in ALLOWED_SOURCES)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _deny_source(self) -> bool:
        addr = self.client_address[0]
        if source_allowed(addr):
            return False
        self._send(403, b'{"error":"source not permitted"}\n', "application/json")
        return True

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self._deny_source():
            return
        path = self.path.rstrip("/") or "/"
        if path == "/":
            self._send(200, cluster_page(), "text/html; charset=utf-8")
            return
        if path in ("/healthz", "/status.json"):
            # Peer agents read this for the node table, so it carries this
            # node's interlink and driver figures.
            self._send(200, json.dumps({
                "node": HOSTNAME, "uptime_s": round(time.time() - STARTED, 1),
                "group": MENTAT_GROUP, "tools": sorted(TOOLS),
                "interconnect": read_interconnect(),
                "driver": read_driver_errors(),
            }, indent=2).encode(), "application/json")
            return
        self._send(404, b"not found\n", "text/plain")

    def do_POST(self):
        if self._deny_source():
            return
        path = self.path.rstrip("/") or "/"
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode() or "{}")
        except ValueError:
            self._send(400, b'{"error":"bad json"}', "application/json")
            return
        if path == "/mcp":
            self._send(200, json.dumps(mcp(payload)).encode(), "application/json")
            return
        self._send(404, b'{"error":"not found"}', "application/json")


def mcp(req: dict) -> dict:
    rid, method = req.get("id"), req.get("method")
    def ok(result): return {"jsonrpc": "2.0", "id": rid, "result": result}

    if method == "initialize":
        return ok({"protocolVersion": "2024-11-05",
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": f"spark-agent@{HOSTNAME}",
                                  "version": "0.2"}})
    if method == "tools/list":
        return ok({"tools": [{"name": n, "description": d,
                              "inputSchema": {"type": "object",
                                              "properties": dict(p)}}
                             for n, (_, d, p) in sorted(TOOLS.items())]})
    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        if name not in TOOLS:
            return ok({"content": [{"type": "text",
                                    "text": f"no tool {name!r} on {HOSTNAME}"}],
                       "isError": True})
        try:
            text = TOOLS[name][0](**(params.get("arguments") or {}))
        except Exception as e:
            return ok({"content": [{"type": "text",
                                    "text": f"{type(e).__name__}: {e}"}],
                       "isError": True})
        return ok({"content": [{"type": "text", "text": text}], "isError": False})
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"no method {method!r}"}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--bind", default="0.0.0.0")
    a = ap.parse_args()
    global LISTEN_PORT
    LISTEN_PORT = a.port
    threading.Thread(target=watch_cluster, daemon=True).start()
    threading.Thread(target=register_loop, daemon=True).start()
    srv = Server((a.bind, a.port), Handler)
    print(f"spark-agent on {a.bind}:{a.port} as {HOSTNAME}, group "
          f"{MENTAT_GROUP}, {len(TOOLS)} tools, logs at {LOG_DIR}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
