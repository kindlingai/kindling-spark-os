# SPDX-License-Identifier: GPL-3.0-or-later WITH LicenseRef-dispram-bundling-exception
# GPL-3.0-or-later (../LICENSE-GPL) with the dispram bundling exception (../BUNDLING-EXCEPTION):
# you may bundle this file into vLLM or an image under that work's own license, as long as this file
# and your changes to it stay under these terms.
"""vLLM general plugin: grow the KV cache into the GB10 display carveout, for stock vLLM images.

Workers add the carveout bytes dispramd can lend to their available KV memory, then build the one
KV backing buffer with the carveout as its tail (see dispram.glued_zeros). Does nothing when the
dispramd socket is absent, or when vLLM already carries ../vllm/kv-cache-from-dispram.patch, which
does the same without patching at runtime.
"""
import logging

logger = logging.getLogger("vllm.dispram")


def register():
    import dispram

    if not dispram.available():
        return
    try:
        import importlib

        import torch
        import vllm.v1.worker.gpu_worker as gw
        import vllm.v1.worker.utils as worker_utils
    except ImportError:
        return
    if getattr(gw.Worker, "_dispram", False) or hasattr(worker_utils, "dispram_reserve_bytes"):
        return
    gw.Worker._dispram = True

    determine = gw.Worker.determine_available_memory

    def determine_available_memory(self):
        avail = determine(self)
        extra = dispram.reserve_bytes()
        logger.info("dispram: adding %.2f GiB of display carveout to %.2f GiB of KV memory",
                    extra / 2**30, avail / 2**30)
        return avail + extra

    gw.Worker.determine_available_memory = determine_available_memory

    # Builds whose V1 runner allocates elsewhere have no worker_utils.allocate_kv_cache; only the V2 hook applies there.
    allocate = getattr(worker_utils, "allocate_kv_cache", None)

    def allocate_kv_cache(kv_cache_config, device, *args, **kwargs):
        zeros = torch.zeros

        def glued(*a, **k):
            # allocate_kv_cache makes exactly one torch.zeros(n, dtype=int8, device=device): the backing buffer.
            if len(a) == 1 and isinstance(a[0], int) and k.get("dtype") is torch.int8:
                t, tail = dispram.glued_zeros(a[0], k.get("device", device))
                logger.info("dispram: KV buffer %.2f GiB, of which %.2f GiB in the display carveout",
                            a[0] / 2**30, tail / 2**30)
                return t
            return zeros(*a, **k)

        torch.zeros = glued
        try:
            return allocate(kv_cache_config, device, *args, **kwargs)
        finally:
            torch.zeros = zeros

    # The V2 model runner allocates one int8 tensor per KV cache tensor (plus one shared backing for packed
    # layouts) instead of one buffer, so the carveout cannot be a single tensor's tail. Glue one buffer for
    # all of them and hand each allocation the next 64 KiB-aligned slice, in the allocator's own order.
    try:
        v2 = importlib.import_module("vllm.v1.worker.gpu.attn_utils")
    except ImportError:
        v2 = None
    v2_allocate = getattr(v2, "_allocate_kv_cache", None)
    if v2_allocate is not None:

        def v2_allocate_kv_cache(kv_cache_config, shared_layers, device):
            sizes, packed = [], False
            for spec in kv_cache_config.kv_cache_tensors:
                if spec.block_stride <= 0:
                    sizes.append(spec.size)
                elif not packed:
                    sizes.append(spec.size)
                    packed = True
            align = 1 << 16
            offsets, total = [], 0
            for size in sizes:
                offsets.append(total)
                total += -(-size // align) * align
            buffer, tail = dispram.glued_zeros(total, device)
            logger.info("dispram: %d KV tensors in one %.2f GiB buffer, of which %.2f GiB in the display carveout",
                        len(sizes), total / 2**30, tail / 2**30)
            slices = iter(zip(offsets, sizes))
            zeros = torch.zeros

            def sliced(*a, **k):
                if len(a) == 1 and isinstance(a[0], int) and k.get("dtype") is torch.int8:
                    offset, size = next(slices)
                    if size != a[0]:
                        raise RuntimeError("dispram: KV allocation order changed (%d bytes, expected %d)" % (a[0], size))
                    return buffer[offset:offset + size]
                return zeros(*a, **k)

            torch.zeros = sliced
            try:
                return v2_allocate(kv_cache_config, shared_layers, device)
            finally:
                torch.zeros = zeros

        v2._allocate_kv_cache = v2_allocate_kv_cache

    # Both model runners import allocate_kv_cache by name, so each module's reference is rebound.
    patched = ["vllm.v1.worker.gpu.attn_utils (V2)"] if v2_allocate is not None else []
    for name in ("vllm.v1.worker.utils", "vllm.v1.worker.gpu_model_runner", "vllm.v1.worker.gpu.attn_utils"):
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        if allocate is not None and getattr(module, "allocate_kv_cache", None) is allocate:
            module.allocate_kv_cache = allocate_kv_cache
            patched.append(name)
    if not patched:
        # Without an allocation hook the extra KV budget would come from ordinary memory.
        gw.Worker.determine_available_memory = determine
        logger.warning("dispram: no KV allocator to hook in this vLLM; plugin inactive")
        return
    logger.info("dispram: KV carveout plugin active in %s", ", ".join(patched))
