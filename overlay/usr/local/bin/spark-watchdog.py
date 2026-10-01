#!/usr/bin/env python3
"""Pet the hardware watchdog only while this machine is still usable.

Two different deaths, and they need two different probes.

FORK DEATH. The kernel keeps running, answers ping and completes TCP
handshakes, while every process blocks in execve under memory pressure.
gx10-2353 died that way three times in 24 hours and each recovery needed
someone to hold the power button. systemd's own RuntimeWatchdogSec does not
catch it: PID 1 is resident and needs neither a fork nor an allocation to write
a keepalive, so it pets happily through exactly the state we care about. The
exec probe covers this, and covers it quickly.

SESSION DEATH. gx10-efcd wedged on 2026-09-16 and the exec probe sailed
straight through it -- not one failure in seven days, including the wedge. The
OOM killer had taken dbus, the user session's systemd, and the rest of the
login plumbing, while ~6 GiB `cicc` compiler processes were respawned every few
seconds. Forking a megabyte and a static binary still worked fine; starting a
SESSION did not. sshd accepted the connection and never sent a banner. The box
needed a manual power cycle.

The lesson is which quantity to measure. The exec probe measures the CAUSE
(is memory available), and the cause is legitimately present during healthy
work: a cold model compile starves this box for minutes and must not be reset.
The session probe measures the SYMPTOM (can anyone still get in), which is
false in a wedge and TRUE during a heavy-but-healthy boot. That is the
discrimination the exec probe cannot make at any threshold.

Session death is therefore gated on two signals at once -- no ssh banner AND
sustained full memory pressure -- over a much longer window than fork death,
because a false positive here costs a serving box its uptime and its warm JIT
caches.

Safe to kill: nowayout is 0 on these boxes, so if this process dies the
descriptor closes and the watchdog disarms rather than rebooting a machine
whose only fault was a crash in here.
"""
import fcntl, os, signal, socket, struct, subprocess, sys, threading, time

DEV = "/dev/watchdog"
WDIOC_SETTIMEOUT = 0xC0045706
TIMEOUT_S = int(os.environ.get("WATCHDOG_TIMEOUT_S", "60"))
INTERVAL_S = int(os.environ.get("WATCHDOG_INTERVAL_S", "15"))
# Consecutive probe failures tolerated before the pets stop. The hardware then
# resets one timeout later, so the real delay to reset is FAILURES*INTERVAL +
# TIMEOUT, and on SBSA the second stage doubles that again.
FAILURES = int(os.environ.get("WATCHDOG_FAILURES", "4"))
# Touch this to keep petting no matter what the probe says. For planned work
# that makes a box briefly unresponsive and must not reboot it.
HOLD = "/run/spark-watchdog-hold"
# What the probe runs. The default is the cheapest possible exec; point it at
# something absent to exercise the give-up path deliberately.
PROBE_CMD = os.environ.get("WATCHDOG_PROBE_CMD", "/bin/true")

# --- session-death detection -------------------------------------------------
# Where to look for a login banner. Localhost on purpose: this asks "can a
# session still start HERE", and must not depend on the network.
SESSION_PORT = int(os.environ.get("WATCHDOG_SESSION_PORT", "22"))
SESSION_TIMEOUT_S = int(os.environ.get("WATCHDOG_SESSION_TIMEOUT_S", "10"))
# Consecutive session failures tolerated. Much higher than FAILURES: a cold
# torch.compile/FlashInfer warmup can starve this box for minutes and must ride
# it out, while a real wedge lasts until someone intervenes, so waiting is
# nearly free. 20 * 15s = 5 minutes of continuous, corroborated failure.
SESSION_FAILURES = int(os.environ.get("WATCHDOG_SESSION_FAILURES", "20"))
# /proc/pressure/memory "full" is the share of time EVERY task was stalled on
# memory. Reading it needs no fork and no allocation, so it still answers in
# the state where nothing else does. A healthy node reads 0.00; a box thrashing
# itself to death sits near 100. 80 leaves room for a genuinely busy compile.
PRESSURE_FILE = os.environ.get("WATCHDOG_PRESSURE_FILE", "/proc/pressure/memory")
PRESSURE_FULL_MIN = float(os.environ.get("WATCHDOG_PRESSURE_FULL_MIN", "80"))
# Set to 1 to escalate on a missing banner ALONE, without pressure agreeing.
# Off by default: one signal is how you reset a box that was only restarting
# sshd.
SESSION_ALONE = os.environ.get("WATCHDOG_SESSION_ALONE", "0") == "1"
# A healthy boot used to log nothing at all -- the seven days spanning the efcd
# wedge produced exactly two lines, which left no forensics whatsoever. Log a
# heartbeat every N intervals instead (20 * 15s = 5 min).
HEARTBEAT_EVERY = int(os.environ.get("WATCHDOG_HEARTBEAT_EVERY", "20"))
# Give-ups are recorded here so a box that keeps resetting stops resetting.
# The record survives because giving up syncs before it waits for the reset.
STATE = "/var/lib/spark-watchdog/resets"
LOOP_WINDOW_S = int(os.environ.get("WATCHDOG_LOOP_WINDOW_S", "3600"))
LOOP_MAX = int(os.environ.get("WATCHDOG_LOOP_MAX", "2"))


def log(msg):
    print(f"spark-watchdog: {msg}", flush=True)


# os.sync() is sync(2) itself, no subprocess. What is fragile is starting a
# thread to run it: that allocates a stack at the exact moment memory is gone.
# So the thread is created once, while the box is healthy, and afterwards
# waking it costs nothing.
_sync_now = threading.Event()
_sync_why = ["startup"]


def recent_resets() -> list[float]:
    try:
        with open(STATE) as f:
            stamps = [float(l) for l in f if l.strip()]
    except (OSError, ValueError):
        return []
    return [t for t in stamps if time.time() - t < LOOP_WINDOW_S]


def record_reset():
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, "a") as f:
            f.write(f"{time.time()}\n")
    except OSError as e:
        log(f"could not record the reset: {e}")


def _sync_worker():
    while True:
        _sync_now.wait()
        _sync_now.clear()
        t0 = time.monotonic()
        try:
            os.sync()
            log(f"sync completed in {time.monotonic() - t0:.1f}s ({_sync_why[0]})")
        except Exception as e:                 # noqa: BLE001
            log(f"sync failed ({_sync_why[0]}): {e}")


def request_sync(why: str):
    """Flush the journal without blocking the probe loop.

    A sync on a machine this far gone can block for longer than the watchdog
    will wait, and it must not stall probing or delay the reset. Best effort:
    if it does not finish we are no worse off than the power cycles that lost
    the last few minutes of journal.
    """
    _sync_why[0] = why
    _sync_now.set()


def probe_exec() -> bool:
    """True if this machine can still allocate and start a process.

    Catches fork death. Deliberately cheap, which is also its limit: it cannot
    tell a wedged box from a busy one, because a megabyte and a static binary
    keep succeeding long after the machine has stopped being useful.
    """
    try:
        bytearray(1 << 20)
        subprocess.run([PROBE_CMD], timeout=10, check=True)
        return True
    except Exception as e:                     # noqa: BLE001 - any failure counts
        log(f"exec probe failed: {type(e).__name__}: {e}")
        return False


def probe_session():
    """Can sshd still get far enough to greet a connection?

    Returns True (greeted), False (accepted but silent -- the wedge), or None
    (not applicable, do not judge).

    No handshake, no keys, no auth: connect and read the version string. sshd
    forks a child per connection and that child writes the banner, so a banner
    proves fork, exec, dynamic linking and a write all still work -- a far
    higher bar than execing /bin/true, and the exact bar a human needs to get
    in and fix the box.

    The refused/silent split is what keeps this safe. In the efcd wedge port 22
    was OPEN and ACCEPTING the whole time and the failure was precisely
    "timed out during banner exchange". Conversely a stopped or restarting sshd
    REFUSES, which is an administrative state and no reason to reset anything.
    Anything unexpected returns None rather than voting to reset.
    """
    sock = None
    try:
        sock = socket.create_connection(("127.0.0.1", SESSION_PORT),
                                        timeout=SESSION_TIMEOUT_S)
        sock.settimeout(SESSION_TIMEOUT_S)
        banner = sock.recv(256)
        if banner.startswith(b"SSH-"):
            return True
        # Connected, spoke, but not as sshd. Not our failure to judge.
        log(f"session probe: unexpected banner {banner[:32]!r}")
        return None
    except ConnectionRefusedError:
        return None                            # sshd is down on purpose
    except (socket.timeout, TimeoutError):
        log("session probe: accepted but no banner "
            f"within {SESSION_TIMEOUT_S}s")
        return False
    except OSError as e:
        log(f"session probe: {type(e).__name__}: {e}")
        return None
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def pressure_full():
    """`full avg60` from /proc/pressure/memory, or None if unreadable.

    A plain file read: no fork, no allocation, so it keeps answering in the
    state where the exec probe is the only other thing still working.
    """
    try:
        with open(PRESSURE_FILE) as f:
            for line in f:
                if line.startswith("full "):
                    for field in line.split():
                        if field.startswith("avg60="):
                            return float(field.split("=", 1)[1])
    except (OSError, ValueError):
        return None
    return None


def mem_available_mb():
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def main():
    # A watchdog that reboots a box over and over is worse than one that does
    # nothing: it denies anyone the chance to log in and look. Two resets in an
    # hour means the fault is not something a reset clears.
    prior = recent_resets()
    if len(prior) >= LOOP_MAX:
        log(f"NOT arming: {len(prior)} watchdog resets in the last "
            f"{LOOP_WINDOW_S}s. This box needs a human, not another reset. "
            f"Clear {STATE} to re-arm.")
        return 0

    fd = os.open(DEV, os.O_WRONLY)
    try:
        fcntl.ioctl(fd, WDIOC_SETTIMEOUT, struct.pack("i", TIMEOUT_S))
    except OSError as e:
        log(f"could not set timeout, using the driver default: {e}")

    disarm = {"wanted": False}

    def on_term(_sig, _frm):
        disarm["wanted"] = True

    threading.Thread(target=_sync_worker, daemon=True, name="sync").start()
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    log(f"armed: timeout={TIMEOUT_S}s interval={INTERVAL_S}s "
        f"exec_failures={FAILURES} probe={PROBE_CMD} | "
        f"session_failures={SESSION_FAILURES} port={SESSION_PORT} "
        f"pressure_full>={PRESSURE_FULL_MIN} alone={int(SESSION_ALONE)}")

    def give_up(why: str):
        """Stop petting and wait for the hardware. Never returns."""
        log(f"{why}; petting stops, hardware resets in ~{TIMEOUT_S}s")
        record_reset()
        # Blocking is fine here: pets have stopped, so the reset is already on
        # its way and nothing is waiting on this loop.
        try:
            os.sync()
            log("final sync completed")
        except Exception as e:                 # noqa: BLE001
            log(f"final sync failed: {e}")
        # Hold the descriptor OPEN and stop petting. nowayout is 0 on these
        # boxes, so closing it would disarm the watchdog and cancel the reset
        # -- the opposite of what is wanted here. Exiting would close it too,
        # hence the wait.
        while True:
            time.sleep(TIMEOUT_S)
            log("still waiting for the hardware reset")

    consec_exec = 0
    consec_session = 0
    ticks = 0
    while not disarm["wanted"]:
        ticks += 1
        held = os.path.exists(HOLD)
        exec_ok = True if held else probe_exec()
        session = None if held else probe_session()
        press = pressure_full()

        # --- fork death -----------------------------------------------------
        if exec_ok:
            if consec_exec:
                log(f"exec probe recovered after {consec_exec} failure(s)")
            consec_exec = 0
        else:
            consec_exec += 1
            if consec_exec == 1:
                # Earliest point at which we know something is wrong, and the
                # likeliest to still succeed.
                request_sync("first exec probe failure")

        # --- session death --------------------------------------------------
        # Two signals, because either one alone has a benign explanation: a
        # silent sshd can be a restart, and high pressure alone is what a
        # healthy cold compile looks like. Together they are the wedge.
        starved = press is not None and press >= PRESSURE_FULL_MIN
        if session is False and (starved or SESSION_ALONE):
            consec_session += 1
            if consec_session == 1:
                request_sync("first session probe failure")
                log(f"session probe failing with memory pressure full={press}; "
                    f"counting to {SESSION_FAILURES} before giving up")
        else:
            if consec_session:
                log(f"session probe recovered after {consec_session} failure(s)")
            consec_session = 0

        if HEARTBEAT_EVERY and ticks % HEARTBEAT_EVERY == 0:
            log(f"heartbeat: exec={'ok' if exec_ok else 'FAIL'} "
                f"session={ {True: 'ok', False: 'SILENT', None: 'n/a'}[session] } "
                f"pressure_full={press} mem_available_mb={mem_available_mb()} "
                f"streak=({consec_exec},{consec_session})")

        if consec_exec >= FAILURES:
            give_up(f"{consec_exec} consecutive exec failures")
        if consec_session >= SESSION_FAILURES:
            give_up(f"{consec_session} consecutive session failures "
                    f"({consec_session * INTERVAL_S}s) with memory pressure "
                    f"full={press}")

        # Petting policy, and the subtle part. The hardware resets TIMEOUT_S
        # after the LAST pet, not after the last failure, so withholding early
        # would make every threshold collapse to TIMEOUT_S. Fork death wants
        # that -- FAILURES*INTERVAL is deliberately sized to match TIMEOUT_S --
        # but session death must ride out a multi-minute compile, so it keeps
        # petting the whole time it counts and only stops at give_up() above.
        if consec_exec:
            log(f"withholding pet (exec {consec_exec}/{FAILURES})")
        else:
            os.write(fd, b"\0")
        time.sleep(INTERVAL_S)

    # "V" is the magic close: it disarms rather than leaving the box to reset
    # while nothing is petting.
    log("disarming on signal")
    os.write(fd, b"V")
    os.close(fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
