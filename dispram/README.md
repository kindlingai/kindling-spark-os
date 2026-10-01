# dispram

Lend the GB10's display carveout, 2 GiB that the driver reserves and never uses on that chip, to
CUDA processes as ordinary device memory.

## Why the memory is free

The firmware reserves `DISPLAY_FRM` for the display: 2046 MiB at `0x280200000` on the boxes checked
so far. `NV2080_CTRL_CMD_FB_GET_CARVEOUT_REGION_INFO` reports it. The NVIDIA driver builds a heap
over it (`memmgrCreateScanoutCarveoutHeap_GB10B`), but its scanout allocation paths are gated on
`PDB_PROP_GPU_IS_SOC_SDM`. Only GB20B and GB20C set that property, in 580.178.04 and still in 615.
So on GB10 nothing allocates from the heap. The GPU's SMMU maps the range straight through (an IORT
RMR), and the kernel keeps it out of system RAM. The firmware offers no setting below 2 GB.

## How it is lent

- `dispramd.py` (root) opens an RM client and, for each request, wraps a slice of the range in an
  `NV01_MEMORY_LIST_SYSTEM` object. It exports that object as a file descriptor with
  `NV0000_CTRL_CMD_OS_UNIX_EXPORT_OBJECT_TO_FD`, and passes the fd over a Unix socket with
  `SCM_RIGHTS`. Creating memory lists needs root (`RS_FLAGS_ALLOC_PRIVILEGED`), which containers
  usually lack. Importing the fd needs no privilege.
- `rmlist.c` (built as `librmlist.so`) holds the RM calls. It compiles against
  [open-gpu-kernel-modules](https://github.com/NVIDIA/open-gpu-kernel-modules) at the driver's
  exact tag, because those structures change between releases.
- `python/dispram.py` is the client. `glued_zeros(nbytes, device)` maps one virtual range with the
  CUDA VMM API: ordinary device memory (`cuMemCreate`) in front, and the carveout slice
  (`cuMemImportFromShareableHandle`) behind it. It returns that range as a zeroed int8 tensor.

Each request carries `"key": "kindlingai_1"`. Slices are 2 MiB granular, and they are freed when
the client closes its socket, normally at process exit.

The method depends on RM internals, so `dispramd` runs only on the driver release named in
`DISPRAM_DRIVER`, the one its stack validated. On any other release it exits with status 3.

## Measured on spark-f1ff (580.178.04)

- All 2046 MiB passed a fill and verify: 0 wrong words out of 268 million.
- Copies run at 233–235 GB/s into and out of it, the same as `cudaMalloc` memory.
- System RAM does not change when it is imported.
- A 640 MiB range glued from 512 MiB of device memory and two 64 MiB carveout slices passed
  across both seams.
- A 3 GiB tensor with a 2042 MiB carveout tail, built in an unprivileged container through
  dispramd, passed a pattern check.

## vLLM

`vllm/kv-cache-from-dispram.patch` makes vLLM put its KV cache's tail in the carveout. It targets
vLLM `ddd6fbca` (0.30.1rc1.dev193). vLLM keeps all KV in one backing buffer, so the patch changes
two places:

- `allocate_kv_cache` builds that buffer with `dispram.glued_zeros` when dispramd is reachable.
- `Worker.determine_available_memory` adds the bytes dispram can lend, so vLLM sizes more blocks.

Without dispramd, or with `DISPRAM_DISABLE=1`, vLLM behaves as before. Apply it to an installed
vLLM with:

    patch -p1 -d /usr/local/lib/python3.12/dist-packages < vllm/kv-cache-from-dispram.patch

For stock images, `python/dispram_vllm.py` does the same at runtime as a vLLM general plugin. It
stands aside when the patch is present. Run either way with:

    -v /run/dispram:/run/dispram -v /opt/kindling/dispram/python:/opt/dispram:ro -e PYTHONPATH=/opt/dispram

Under tensor parallelism, every rank should have dispram, because vLLM takes the smallest KV budget
across ranks.

## License

AGPL-3.0 (`LICENSE`). `rmlist.c` includes NVIDIA's open-gpu-kernel-modules headers, which are MIT.
