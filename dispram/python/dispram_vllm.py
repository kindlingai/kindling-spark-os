# SPDX-License-Identifier: AGPL-3.0-or-later OR CC0-1.0
# This vLLM plugin is yours under either license: AGPL-3.0-or-later (../LICENSE) or CC0-1.0
# (../LICENSE-CC0). If you use it, please credit dispram from kindlingai/kindling-spark-os. Under
# CC0 that is a request, not a condition.
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

    allocate = worker_utils.allocate_kv_cache

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

    # Both model runners import allocate_kv_cache by name, so each module's reference is rebound.
    patched = []
    for name in ("vllm.v1.worker.utils", "vllm.v1.worker.gpu_model_runner", "vllm.v1.worker.gpu.attn_utils"):
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        if getattr(module, "allocate_kv_cache", None) is allocate:
            module.allocate_kv_cache = allocate_kv_cache
            patched.append(name)
    logger.info("dispram: KV carveout plugin active in %s", ", ".join(patched))
