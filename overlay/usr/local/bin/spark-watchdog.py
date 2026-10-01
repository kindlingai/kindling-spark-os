#!/usr/bin/env python3
"""Pet the hardware watchdog only while this machine can still start a process.

The failure this exists for is not a hang: the kernel keeps running, answers
ping and completes TCP handshakes, while every process blocks in execve under
memory pressure. gx10-2353 died that way three times in 24 hours and each
recovery needed someone to hold the power button.

systemd's own RuntimeWatchdogSec does not catch it. PID 1 is resident and
needs neither a fork nor an allocation to write a keepalive, so it pets
happily through exactly the state we care about -- journald was still logging
seconds before the last two deaths. The probe here forks and execs instead,
because that is the thing that stops working.

Safe to kill: nowayout is 0 on these boxes, so if this process dies the
descriptor closes and the watchdog disarms rather than rebooting a machine
whose only fault was a crash in here.
"""
import fcntl, os, signal, struct, subprocess, sys, threading, time

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


def probe() -> bool:
    """True if this machine can still allocate and start a process."""
    try:
        bytearray(1 << 20)
        subprocess.run([PROBE_CMD], timeout=10, check=True)
        return True
    except Exception as e:                     # noqa: BLE001 - any failure counts
        log(f"probe failed: {type(e).__name__}: {e}")
        return False


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
        f"failures={FAILURES} probe={PROBE_CMD}")

    consecutive = 0
    while not disarm["wanted"]:
        if os.path.exists(HOLD) or probe():
            if consecutive:
                log(f"probe recovered after {consecutive} failure(s)")
            consecutive = 0
            os.write(fd, b"\0")
        else:
            consecutive += 1
            if consecutive == 1:
                # Earliest point at which we know something is wrong, and the
                # likeliest to still succeed.
                request_sync("first probe failure")
            if consecutive >= FAILURES:
                log(f"{consecutive} consecutive failures; petting stops, "
                    f"hardware resets in ~{TIMEOUT_S}s")
                record_reset()
                # Blocking is fine here: pets have stopped, so the reset is
                # already on its way and nothing is waiting on this loop.
                try:
                    os.sync()
                    log("final sync completed")
                except Exception as e:         # noqa: BLE001
                    log(f"final sync failed: {e}")
                # Hold the descriptor OPEN and stop petting. nowayout is 0 on
                # these boxes, so closing it would disarm the watchdog and
                # cancel the reset -- the opposite of what is wanted here.
                # Exiting would close it too, hence the wait.
                while True:
                    time.sleep(TIMEOUT_S)
                    log("still waiting for the hardware reset")
            log(f"withholding pet ({consecutive}/{FAILURES})")
        time.sleep(INTERVAL_S)

    # "V" is the magic close: it disarms rather than leaving the box to reset
    # while nothing is petting.
    log("disarming on signal")
    os.write(fd, b"V")
    os.close(fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
