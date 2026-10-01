#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hand out slices of the GB10 display carveout as CUDA-importable fds.

The RM reports a 2046 MiB DISPLAY_FRM carveout that nothing allocates from on GB10. Root can wrap
part of it in an RM memory-list object and export that as an fd, which any process can import with
cuMemImportFromShareableHandle (POSIX fd) or cudaImportExternalMemory (opaque fd), no privilege
needed. This daemon does the root half and keeps slices from overlapping.

Protocol: SOCK_SEQPACKET, one JSON object per message. Every request carries "key": "kindlingai_1";
dispramd answers anything else with an error and closes the connection.
  {"op": "info"}              -> {"base", "size", "free", "largest"}
  {"op": "alloc", "size": N}  -> {"ok": true, "base", "size"} with the fd attached (SCM_RIGHTS),
                                 or {"ok": false, "error"}
A connection's slices are freed when it closes, so a client holds its socket for as long as it
uses the memory.

Lent slices are also recorded in /run/dispram/lent.json with the borrowing process (its pid, from
the socket's peer credentials, and its start time). A restarted dispramd reserves every recorded
slice whose borrower is still running, and frees it when that process exits, so it never lends a
slice that is still mapped. /run does not survive a reboot, and neither do the borrowers.

The method leans on RM internals (the carveout-info control, memory-list objects, fd export), so it
runs only on the driver release named in DISPRAM_DRIVER, the one its stack validated. On any other
driver it exits with status 3.
"""
import ctypes
import json
import os
import selectors
import socket
import struct
import sys

GRAN = 2 << 20
KEY = "kindlingai_1"
SOCK = os.environ.get("DISPRAM_SOCKET", "/run/dispram/dispram.sock")
STATE = os.path.join(os.path.dirname(SOCK), "lent.json")
LIB = os.environ.get("DISPRAM_LIB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "librmlist.so"))


def log(*a):
    print("dispramd:", *a, file=sys.stderr, flush=True)


class Carveout:
    def __init__(self):
        self.rm = ctypes.CDLL(LIB)
        if self.rm.rm_open():
            sys.exit("dispramd: cannot open an RM client (needs root and the nvidia driver)")
        base, size = ctypes.c_ulonglong(), ctypes.c_ulonglong()
        if self.rm.rm_display_frm(ctypes.byref(base), ctypes.byref(size)):
            sys.exit("dispramd: the RM reports no DISPLAY_FRM carveout")
        self.base, self.size = base.value, size.value
        self.free = [(self.base, self.base + self.size)]  # sorted, disjoint [start, end)

    def inside(self, start, size):
        """Whether [start, start + size) is a non-empty range inside DISPLAY_FRM."""
        return size > 0 and self.base <= start and start + size <= self.base + self.size

    def alloc(self, size):
        """A (start, size) slice of at least size bytes, rounded up to GRAN, or None.

        Raises ValueError for a size that is not a positive int no larger than the carveout. An
        unchecked negative size moved the free list below DISPLAY_FRM, so later slices could cover
        ordinary RAM.
        """
        if type(size) is not int or not 0 < size <= self.size:
            raise ValueError(f"size must be an integer from 1 to {self.size}")
        size = -(-size // GRAN) * GRAN
        for i, (s, e) in enumerate(self.free):
            if e - s >= size:
                self.free[i:i + 1] = [(s + size, e)] if e - s > size else []
                if not self.inside(s, size):  # cannot happen while free stays inside the carveout
                    raise RuntimeError(f"allocator produced 0x{s:x} + {size}, outside DISPLAY_FRM")
                return s, size
        return None

    def reserve(self, start, size):
        """Take [start, start + size) out of the free list. Returns False if any of it is not free."""
        if not self.inside(start, size):
            return False
        for i, (s, e) in enumerate(self.free):
            if s <= start and start + size <= e:
                self.free[i:i + 1] = [r for r in ((s, start), (start + size, e)) if r[0] < r[1]]
                return True
        return False

    def release(self, start, size):
        if not self.inside(start, size):
            raise RuntimeError(f"release of 0x{start:x} + {size}, outside DISPLAY_FRM")
        self.free.append((start, start + size))
        self.free.sort()
        merged = []
        for s, e in self.free:
            if merged and merged[-1][1] == s:
                merged[-1] = (merged[-1][0], e)
            else:
                merged.append((s, e))
        self.free = merged

    def stats(self):
        sizes = [e - s for s, e in self.free]
        return {"base": self.base, "size": self.size, "free": sum(sizes), "largest": max(sizes, default=0)}


def peer(conn):
    """The (pid, start time) of the process at the other end of a Unix socket, or None."""
    try:
        pid = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[0]
        return pid, start_time(pid)
    except (OSError, IndexError, ValueError):
        return None


def start_time(pid):
    """A process's start time in clock ticks since boot (field 22 of /proc/PID/stat), so a reused pid
    is not mistaken for the process it replaced."""
    with open(f"/proc/{pid}/stat") as f:
        return int(f.read().rsplit(")", 1)[1].split()[19])


def alive(pid, started):
    try:
        return start_time(pid) == started
    except (OSError, IndexError, ValueError):
        return False


def check_driver():
    want = os.environ.get("DISPRAM_DRIVER", "")
    try:
        have = open("/proc/driver/nvidia/version").readline()
    except OSError:
        have = ""
    if not want or f" {want} " not in have:
        log(f"driver {have.strip() or 'not loaded'} is not the validated release {want or '(none set)'}; lending nothing")
        sys.exit(3)


def main():
    check_driver()
    co = Carveout()
    log(f"DISPLAY_FRM 0x{co.base:x} + {co.size >> 20} MiB")
    os.makedirs(os.path.dirname(SOCK), exist_ok=True)
    try:
        os.unlink(SOCK)
    except FileNotFoundError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    srv.bind(SOCK)
    os.chmod(SOCK, 0o666)
    srv.listen()
    sel = selectors.DefaultSelector()
    sel.register(srv, selectors.EVENT_READ)
    owned = {}  # conn -> [(start, size, rm handle, borrower)]
    # Slices lent before a restart, whose borrowers still run: [(start, size, (pid, start time))].
    inherited = []

    def save():
        rows = [{"start": s, "size": z, "pid": b[0], "started": b[1]}
                for slices in owned.values() for s, z, _, b in slices if b]
        rows += [{"start": s, "size": z, "pid": b[0], "started": b[1]} for s, z, b in inherited]
        tmp = STATE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(rows, f)
        os.replace(tmp, STATE)

    try:
        rows = json.load(open(STATE))
    except (OSError, ValueError):
        rows = []
    for r in rows:
        try:
            start, size, b = int(r["start"]), int(r["size"]), (int(r["pid"]), int(r["started"]))
        except (KeyError, TypeError, ValueError):
            continue
        if alive(*b) and co.reserve(start, size):
            inherited.append((start, size, b))
            log(f"kept 0x{start:x} + {size >> 20} MiB, still mapped by pid {b[0]}")
    save()

    def reap():
        """Free inherited slices whose borrowers have exited."""
        for item in [i for i in inherited if not alive(*i[2])]:
            inherited.remove(item)
            co.release(item[0], item[1])
            log(f"freed 0x{item[0]:x} + {item[1] >> 20} MiB, pid {item[2][0]} has exited")
            save()

    def close(conn):
        for start, size, handle, _ in owned.pop(conn, []):
            co.rm.rm_free(ctypes.c_uint(handle))
            co.release(start, size)
            log(f"freed 0x{start:x} + {size >> 20} MiB")
        save()
        sel.unregister(conn)
        conn.close()

    def reply(conn, obj, fds=()):
        socket.send_fds(conn, [json.dumps(obj).encode()], list(fds))

    def handle(conn, msg):
        """Answer one request. Returns False when the connection should close."""
        try:
            req = json.loads(msg)
        except ValueError:
            req = None
        if not isinstance(req, dict) or req.get("key") != KEY:
            reply(conn, {"ok": False, "error": "missing or unknown key"})
            return False
        op = req.get("op")
        if op == "info":
            reply(conn, co.stats())
        elif op == "alloc":
            try:
                got = co.alloc(req.get("size"))
            except ValueError as e:
                reply(conn, {"ok": False, "error": str(e)})
                return True
            if got is None:
                reply(conn, {"ok": False, "error": "not enough free carveout", **co.stats()})
                return True
            start, size = got
            handle = ctypes.c_uint()
            fd = co.rm.rm_export_range_h(ctypes.c_ulonglong(start), ctypes.c_ulonglong(size), ctypes.byref(handle))
            if fd < 0:
                co.release(start, size)
                reply(conn, {"ok": False, "error": "RM export failed"})
                return True
            owned[conn].append((start, size, handle.value, peer(conn)))
            save()
            try:
                reply(conn, {"ok": True, "base": start, "size": size}, [fd])
            finally:
                os.close(fd)
            log(f"lent 0x{start:x} + {size >> 20} MiB")
        else:
            reply(conn, {"ok": False, "error": "unknown op"})
        return True

    while True:
        events = sel.select(timeout=10 if inherited else None)
        if inherited:
            reap()
        for key, _ in events:
            if key.fileobj is srv:
                conn, _ = srv.accept()
                owned[conn] = []
                sel.register(conn, selectors.EVENT_READ)
                continue
            conn = key.fileobj
            # One client's error closes that client and frees its slices, and leaves the daemon
            # running.
            try:
                msg = conn.recv(4096)
                if msg and handle(conn, msg):
                    continue
            except Exception as e:
                log(f"closing a client after {type(e).__name__}: {e}")
            close(conn)


if __name__ == "__main__":
    main()
