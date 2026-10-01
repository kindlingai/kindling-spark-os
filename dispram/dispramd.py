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

The method leans on RM internals (the carveout-info control, memory-list objects, fd export), so it
runs only on the driver release named in DISPRAM_DRIVER, the one its stack validated. On any other
driver it exits with status 3.
"""
import ctypes
import json
import os
import selectors
import socket
import sys

GRAN = 2 << 20
KEY = "kindlingai_1"
SOCK = os.environ.get("DISPRAM_SOCKET", "/run/dispram/dispram.sock")
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

    def alloc(self, size):
        size = -(-size // GRAN) * GRAN
        for i, (s, e) in enumerate(self.free):
            if e - s >= size:
                self.free[i:i + 1] = [(s + size, e)] if e - s > size else []
                return s, size
        return None

    def release(self, start, size):
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
    owned = {}  # conn -> [(start, size, rm handle)]

    def close(conn):
        for start, size, handle in owned.pop(conn, []):
            co.rm.rm_free(ctypes.c_uint(handle))
            co.release(start, size)
            log(f"freed 0x{start:x} + {size >> 20} MiB")
        sel.unregister(conn)
        conn.close()

    while True:
        for key, _ in sel.select():
            if key.fileobj is srv:
                conn, _ = srv.accept()
                owned[conn] = []
                sel.register(conn, selectors.EVENT_READ)
                continue
            conn = key.fileobj
            try:
                msg = conn.recv(4096)
            except OSError:
                msg = b""
            if not msg:
                close(conn)
                continue
            try:
                req = json.loads(msg)
            except ValueError:
                req = {}
            if not isinstance(req, dict) or req.get("key") != KEY:
                conn.send(json.dumps({"ok": False, "error": "missing or unknown key"}).encode())
                close(conn)
                continue
            if req.get("op") == "info":
                conn.send(json.dumps(co.stats()).encode())
            elif req.get("op") == "alloc":
                got = co.alloc(int(req["size"]))
                if got is None:
                    conn.send(json.dumps({"ok": False, "error": "not enough free carveout", **co.stats()}).encode())
                    continue
                start, size = got
                handle = ctypes.c_uint()
                fd = co.rm.rm_export_range_h(ctypes.c_ulonglong(start), ctypes.c_ulonglong(size), ctypes.byref(handle))
                if fd < 0:
                    co.release(start, size)
                    conn.send(json.dumps({"ok": False, "error": "RM export failed"}).encode())
                    continue
                owned[conn].append((start, size, handle.value))
                socket.send_fds(conn, [json.dumps({"ok": True, "base": start, "size": size}).encode()], [fd])
                os.close(fd)
                log(f"lent 0x{start:x} + {size >> 20} MiB")
            else:
                conn.send(json.dumps({"ok": False, "error": "unknown op"}).encode())


if __name__ == "__main__":
    main()
