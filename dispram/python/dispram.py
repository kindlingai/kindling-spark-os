"""Client for dispramd: build device buffers whose tail lives in the GB10 display carveout.

glued_zeros(nbytes, device) returns a zeroed int8 CUDA tensor of nbytes. Its front is ordinary
device memory from cuMemCreate and its tail is a carveout slice, mapped behind it in one virtual
range. The mappings and the daemon connection live as long as the process.
"""
import ctypes
import json
import os
import socket

SOCK = os.environ.get("DISPRAM_SOCKET", "/run/dispram/dispram.sock")
KEY = "kindlingai_1"
GRAN = 2 << 20
# Slack kept back from what reserve_bytes() reports, so rounding never asks for more than is free.
SLACK = int(os.environ.get("DISPRAM_SLACK_MIB", "4")) << 20

_conn = None
_keep = []  # mapped ranges; never unmapped


def _sock():
    global _conn
    if _conn is None:
        _conn = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        _conn.connect(SOCK)
    return _conn


def available():
    """Whether dispramd is reachable. DISPRAM_DISABLE=1 turns dispram off for this process."""
    return os.environ.get("DISPRAM_DISABLE") != "1" and os.path.exists(SOCK)


def _request(**req):
    s = _sock()
    s.send(json.dumps({"key": KEY, **req}).encode())
    return s


def info():
    return json.loads(_request(op="info").recv(4096))


def reserve_bytes():
    """Carveout bytes a caller can add to its budget and still get from glued_zeros."""
    return max(info()["largest"] - SLACK, 0) // GRAN * GRAN


def _lend(size):
    msg, fds, _, _ = socket.recv_fds(_request(op="alloc", size=size), 4096, 1)
    rep = json.loads(msg)
    if not rep.get("ok"):
        raise RuntimeError(f"dispramd: {rep}")
    return fds[0], rep


class _Prop(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("requestedHandleTypes", ctypes.c_int),
                ("locType", ctypes.c_int), ("locId", ctypes.c_int),
                ("win32HandleMetaData", ctypes.c_void_p), ("allocFlags", ctypes.c_uint64)]


class _Access(ctypes.Structure):
    _fields_ = [("locType", ctypes.c_int), ("locId", ctypes.c_int), ("flags", ctypes.c_int)]


_cu = None


def _cuda():
    global _cu
    if _cu is None:
        _cu = ctypes.CDLL("libcuda.so.1")
        for name in ("cuMemAddressReserve", "cuMemCreate", "cuMemMap", "cuMemSetAccess",
                     "cuMemImportFromShareableHandle", "cuMemsetD8_v2", "cuMemRelease"):
            getattr(_cu, name).restype = ctypes.c_int
    return _cu


def _ck(rc, what):
    if rc:
        raise RuntimeError(f"{what} failed: CUresult {rc}")


def map_glued(nbytes, device_index, tail=None):
    """Map nbytes as one virtual range, ordinary memory in front of a carveout tail.

    tail defaults to as much carveout as fits. Returns (device pointer, mapped bytes, tail bytes).
    """
    cu = _cuda()
    total = -(-nbytes // GRAN) * GRAN
    if tail is None:
        tail = min(reserve_bytes(), total)
    tail = tail // GRAN * GRAN
    plain = total - tail

    prop = _Prop(type=1, requestedHandleTypes=1, locType=1, locId=device_index)  # pinned, posix fd, device
    va = ctypes.c_uint64()
    _ck(cu.cuMemAddressReserve(ctypes.byref(va), ctypes.c_size_t(total), ctypes.c_size_t(GRAN),
                               ctypes.c_uint64(0), ctypes.c_uint64(0)), "cuMemAddressReserve")
    if plain:
        h = ctypes.c_uint64()
        _ck(cu.cuMemCreate(ctypes.byref(h), ctypes.c_size_t(plain), ctypes.byref(prop), ctypes.c_uint64(0)),
            "cuMemCreate")
        _ck(cu.cuMemMap(va, ctypes.c_size_t(plain), ctypes.c_size_t(0), h, ctypes.c_uint64(0)), "cuMemMap plain")
        cu.cuMemRelease(h)  # the mapping holds its own reference
    if tail:
        fd, rep = _lend(tail)
        h = ctypes.c_uint64()
        _ck(cu.cuMemImportFromShareableHandle(ctypes.byref(h), ctypes.c_void_p(fd), 1), "cuMemImportFromShareableHandle")
        os.close(fd)
        _ck(cu.cuMemMap(ctypes.c_uint64(va.value + plain), ctypes.c_size_t(tail), ctypes.c_size_t(0), h,
                        ctypes.c_uint64(0)), "cuMemMap carveout")
        cu.cuMemRelease(h)
    acc = _Access(locType=1, locId=device_index, flags=3)  # read-write
    _ck(cu.cuMemSetAccess(va, ctypes.c_size_t(total), ctypes.byref(acc), ctypes.c_size_t(1)), "cuMemSetAccess")
    _ck(cu.cuMemsetD8_v2(va, ctypes.c_ubyte(0), ctypes.c_size_t(total)), "cuMemsetD8")
    _keep.append((va.value, total))
    return va.value, total, tail


class _Array:
    def __init__(self, ptr, n):
        self.__cuda_array_interface__ = {"shape": (n,), "typestr": "|i1", "data": (ptr, False),
                                         "version": 3, "strides": None}


def glued_zeros(nbytes, device):
    """A zeroed int8 tensor of nbytes on device, with its tail in the carveout."""
    import torch

    device = torch.device(device)
    index = device.index if device.index is not None else torch.cuda.current_device()
    torch.cuda.synchronize(index)
    ptr, _, tail = map_glued(nbytes, index)
    t = torch.as_tensor(_Array(ptr, nbytes), device=device)
    return t, tail
