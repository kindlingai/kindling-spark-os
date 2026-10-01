# SPDX-License-Identifier: GPL-3.0-or-later WITH LicenseRef-dispram-bundling-exception
# GPL-3.0-or-later (../LICENSE-GPL) with the dispram bundling exception (../BUNDLING-EXCEPTION):
# you may bundle this file into vLLM or an image under that work's own license, as long as this file
# and your changes to it stay under these terms.
"""Client for dispramd: build device buffers whose tail lives in the GB10 display carveout.

reserve_bytes() claims carveout for this process while it sizes its memory; glued_zeros(nbytes,
device) then returns a zeroed int8 CUDA tensor of nbytes whose front is ordinary device memory
(cuMemCreate) and whose tail is the claimed slice, mapped behind it in one virtual range. The
mappings and the daemon connection live as long as the process; dispramd frees the slices when the
process exits.
"""
import ctypes
import json
import os
import socket

SOCK = os.environ.get("DISPRAM_SOCKET", "/run/dispram/dispram.sock")
KEY = "kindlingai_1"
GRAN = 2 << 20  # dispramd's slice granularity

_conn = None
_claim = None  # (fd, size): the slice reserve_bytes() claimed and glued_zeros has not used yet
_keep = []  # mapped ranges; never unmapped


def _sock():
    global _conn
    if _conn is None:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            conn.connect(SOCK)
        except OSError:
            conn.close()
            raise
        _conn = conn
    return _conn


def available():
    """Whether dispramd answers. A socket file left by a dead daemon counts as unavailable.

    DISPRAM_DISABLE=1 turns dispram off for this process.
    """
    if os.environ.get("DISPRAM_DISABLE") == "1":
        return False
    try:
        _sock()
    except OSError:
        return False
    return True


def _request(**req):
    s = _sock()
    s.send(json.dumps({"key": KEY, **req}).encode())
    return s


def info():
    return json.loads(_request(op="info").recv(4096))


def _lend(size):
    msg, fds, _, _ = socket.recv_fds(_request(op="alloc", size=size), 4096, 1)
    rep = json.loads(msg)
    if not rep.get("ok"):
        raise RuntimeError(f"dispramd: {rep}")
    return fds[0], rep["size"]


def reserve_bytes():
    """Claim the largest slice dispramd can lend and return its size; 0 without dispram.

    The claim is held for this process until glued_zeros maps it, so two processes sizing their
    memory at the same time cannot both count the same carveout. Later calls return the same claim.
    """
    global _claim
    if _claim is None:
        try:
            size = info()["largest"] // GRAN * GRAN
            if not size:
                return 0
            _claim = _lend(size)
        except (OSError, RuntimeError, ValueError, KeyError):
            return 0
    return _claim[1]


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
                     "cuMemImportFromShareableHandle", "cuMemsetD8_v2", "cuMemRelease",
                     "cuMemGetAllocationGranularity", "cuCtxSynchronize"):
            getattr(_cu, name).restype = ctypes.c_int
    return _cu


def _ck(rc, what):
    if rc:
        raise RuntimeError(f"{what} failed: CUresult {rc}")


def _take_tail(total):
    """The carveout (fd, size) for a buffer of total bytes: this process's claim if it holds one,
    else a slice of at most total bytes. (None, 0) when dispram cannot lend any."""
    global _claim
    if _claim is not None:
        claim, _claim = _claim, None
        return claim
    try:
        want = min(info()["largest"], total) // GRAN * GRAN
        return _lend(want) if want else (None, 0)
    except (OSError, RuntimeError, ValueError, KeyError):
        return None, 0


def map_glued(nbytes, device_index):
    """Map at least nbytes as one virtual range: ordinary memory in front of a carveout tail.

    The tail is this process's claim when it holds one (see reserve_bytes), else as much carveout
    as fits. A claim larger than nbytes is mapped whole, with no ordinary memory in front. Returns
    (device pointer, mapped bytes, tail bytes).
    """
    cu = _cuda()
    prop = _Prop(type=1, requestedHandleTypes=1, locType=1, locId=device_index)  # pinned, posix fd, device
    gran = ctypes.c_size_t()
    _ck(cu.cuMemGetAllocationGranularity(ctypes.byref(gran), ctypes.byref(prop), 0), "cuMemGetAllocationGranularity")
    total = -(-nbytes // GRAN) * GRAN

    fd, tail = None, 0
    # A slice can only be mapped whole, so it must be a multiple of CUDA's granularity.
    if gran.value and GRAN % gran.value == 0:
        fd, tail = _take_tail(total)
    total = max(total, tail)
    plain = total - tail

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
        h = ctypes.c_uint64()
        try:
            _ck(cu.cuMemImportFromShareableHandle(ctypes.byref(h), ctypes.c_void_p(fd), 1),
                "cuMemImportFromShareableHandle")
        finally:
            os.close(fd)
        _ck(cu.cuMemMap(ctypes.c_uint64(va.value + plain), ctypes.c_size_t(tail), ctypes.c_size_t(0), h,
                        ctypes.c_uint64(0)), "cuMemMap carveout")
        cu.cuMemRelease(h)
    acc = _Access(locType=1, locId=device_index, flags=3)  # read-write
    _ck(cu.cuMemSetAccess(va, ctypes.c_size_t(total), ctypes.byref(acc), ctypes.c_size_t(1)), "cuMemSetAccess")
    _ck(cu.cuMemsetD8_v2(va, ctypes.c_ubyte(0), ctypes.c_size_t(total)), "cuMemsetD8")
    # The memset runs on the legacy stream. Wait for it, so no other stream sees the buffer unzeroed.
    _ck(cu.cuCtxSynchronize(), "cuCtxSynchronize")
    _keep.append((va.value, total))
    return va.value, total, tail


class _Array:
    def __init__(self, ptr, n):
        self.__cuda_array_interface__ = {"shape": (n,), "typestr": "|i1", "data": (ptr, False),
                                         "version": 3, "strides": None}


def glued_zeros(nbytes, device):
    """A zeroed int8 tensor of nbytes on device, with its tail in the carveout.

    Returns (tensor, tail bytes). Without dispram the tail is 0 and the tensor is ordinary memory.
    """
    import torch

    device = torch.device(device)
    index = device.index if device.index is not None else torch.cuda.current_device()
    torch.cuda.synchronize(index)
    ptr, _, tail = map_glued(nbytes, index)
    t = torch.as_tensor(_Array(ptr, nbytes), device=device)
    return t, tail
