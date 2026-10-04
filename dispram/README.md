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
- `python/dispram.py` is the client. `reserve_bytes()` claims the largest free slice while a
  process sizes its memory, so two processes sizing at once never count the same carveout.
  `glued_zeros(nbytes, device)` then maps one virtual range with the CUDA VMM API: ordinary device
  memory (`cuMemCreate`) in front, and the claimed slice (`cuMemImportFromShareableHandle`) behind
  it. It returns that range as a zeroed int8 tensor. A claim larger than the buffer is mapped whole.
  If dispramd is not answering, both act as if dispram were absent.

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
- vLLM (nightly ddd6fbca, dots-ocr, the plugin) with a 1 GiB KV budget built a 2.99 GiB,
  112,128-token cache, 1.99 GiB of it in the carveout. A 1 GiB budget alone could not start at
  65,536 tokens. Against a baseline with the same 2.99 GiB in ordinary memory, 10 greedy completions
  (9 to 14,041 prompt tokens, sent one at a time) were identical. The KV layout was layer-major,
  so every request used the carveout for its last layers.
- vLLM's V2 model runner (`vllm/v1/worker/gpu`), serving a 3.25 bpw GLM-5.3 at TP=4 with the
  plugin: 101 KV tensors in one 20.63 GiB buffer, 2.00 GiB of it in the carveout. The KV cache grew
  from 365,440 to 404,608 tokens with decode and prefill speed unchanged, and KL against runs
  without dispram (0.017) matched run-to-run noise. Greedy text is no test on that stack: two boots
  without dispram already differ.

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
stands aside when the patch is present. vLLM's V2 model runner allocates one tensor per KV cache
tensor instead of one buffer, so for it the plugin glues one buffer for all of them and hands each
its slice. If it finds neither allocator, it leaves the KV budget alone. Run either way with:

    -v /run/dispram:/run/dispram -v /opt/kindling/dispram/python:/opt/dispram:ro -e PYTHONPATH=/opt/dispram

Append `/opt/dispram` to the image's own `PYTHONPATH` if it sets one.

Under tensor parallelism, every rank should have dispram, because vLLM takes the smallest KV budget
across ranks.

## Prior work

Independently discovered by the author of
[this NVIDIA developer forum post](https://forums.developer.nvidia.com/t/deepseek-v4-1-flash-for-2x-dgx-spark-exl3-3bpw-3m-kv-cache-c6-new-2gb-free-ram-unlock-for-all-gb10s/383583),
which describes a 2 GB free-RAM unlock for all GB10s and came first. dispram is a later,
independent rediscovery.

## License

- `dispramd.py` and `rmlist.c`, the server side: AGPL-3.0 (`LICENSE`).
- `python/dispram.py`, `python/dispram_vllm.py` and `vllm/kv-cache-from-dispram.patch`, which run
  inside someone else's vLLM process: GPL-3.0-or-later (`LICENSE-GPL`) with the dispram bundling
  exception (`BUNDLING-EXCEPTION`). You may bundle them into vLLM or a container image under that
  work's own license, as long as these files and your changes to them stay under GPL with the
  exception, with their notices kept.

The client reaches dispramd only over its Unix socket, so the two sides stay separate works.
`rmlist.c` includes NVIDIA's open-gpu-kernel-modules headers, which are MIT.
