#!/usr/bin/env python3
"""sparkos-memtest logic and smoke test: python3 tools/test-sparkos-memtest.py"""
import importlib.machinery
import os
import struct
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "../overlay/usr/local/sbin/sparkos-memtest")
M = importlib.machinery.SourceFileLoader("sparkos_memtest", SCRIPT).load_module()


class MemtestUnit(unittest.TestCase):
    def test_parse_pagemap_entry_4k_and_64k(self):
        # Present bit (63) set, PFN 0x8ee68, 64 KiB page -> physical 0x8ee68c6bc
        raw = struct.pack("<Q", (1 << 63) | 0x8EE68)
        self.assertEqual(M.parse_pagemap_entry(raw, 0xE0D81C25C6BC, 65536), 0x8EE68C6BC)
        # 4 KiB page size
        raw_4k = struct.pack("<Q", (1 << 63) | 0x12345)
        self.assertEqual(M.parse_pagemap_entry(raw_4k, 0x7FFF0000A678, 4096), 0x12345678)
        # Not present or zero PFN -> None
        self.assertIsNone(M.parse_pagemap_entry(struct.pack("<Q", 0x8EE68), 0x1000, 65536))
        self.assertIsNone(M.parse_pagemap_entry(struct.pack("<Q", 1 << 63), 0x1000, 65536))
        self.assertIsNone(M.parse_pagemap_entry(b"\x00\x01", 0x1000, 65536))

    def test_parse_patterns(self):
        self.assertEqual(M.parse_patterns("00,ff,55,aa,0f,f0"), (0x00, 0xFF, 0x55, 0xAA, 0x0F, 0xF0))
        with self.assertRaises(ValueError):
            M.parse_patterns("100")
        with self.assertRaises(ValueError):
            M.parse_patterns("")

    def test_parse_meminfo(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("MemTotal:       129798784 kB\nMemAvailable:   124429312 kB\nHardwareCorrupted: 16384 kB\n")
            path = f.name
        try:
            info = M.parse_meminfo(path)
            self.assertEqual(info["MemTotal"], 129798784 * 1024)
            self.assertEqual(info["MemAvailable"], 124429312 * 1024)
            self.assertEqual(info["HardwareCorrupted"], 16384 * 1024)
        finally:
            os.unlink(path)

    def test_plan_allocation_auto_and_guardrails(self):
        # Idle 128 GB box with ~118 GiB available, 8 GiB reserve -> ~110 GiB across 16 workers
        workers, per_w, total = M.plan_allocation(118 * M.GIB, None, 16, reserve_gib=8.0, chunk_mib=64)
        self.assertEqual(workers, 16)
        self.assertEqual(per_w % (64 * M.MIB), 0)
        self.assertEqual(total, workers * per_w)
        self.assertGreaterEqual(total, 109 * M.GIB)
        self.assertLessEqual(total, 110 * M.GIB)

        # Small explicit run shrinks worker count instead of zeroing chunks
        workers, per_w, total = M.plan_allocation(16 * M.GIB, 128, 16, chunk_mib=64)
        self.assertEqual((workers, per_w, total), (2, 64 * M.MIB, 128 * M.MIB))

        # Refuses explicit size within 2 GiB of MemAvailable unless --force
        with self.assertRaises(ValueError):
            M.plan_allocation(10 * M.GIB, 9 * 1024, 4, chunk_mib=64, force=False)
        workers, per_w, total = M.plan_allocation(10 * M.GIB, 9 * 1024, 4, chunk_mib=64, force=True)
        self.assertEqual(total, 9 * M.GIB)

    def test_verify_chunk_detects_bit_flip_and_rewrites(self):
        chunk_bytes = 4096
        blk = bytes([0xFF]) * chunk_bytes
        buf = bytearray(blk)
        self.assertEqual(M.verify_chunk(buf, 0x100000, 0, chunk_bytes, 0xFF, blk, pagemap_fn=lambda v: v + 0x500000), [])

        # Inject a 2-bit decay (0xFF -> 0x7D, xor 0x82) at offset 0x6BC
        buf[0x6BC] = 0x7D
        hits = M.verify_chunk(buf, 0x100000, 0, chunk_bytes, 0xFF, blk, pagemap_fn=lambda v: v + 0x500000)
        self.assertEqual(len(hits), 1)
        h = hits[0]
        self.assertEqual(h["chunk_off"], 0x6BC)
        self.assertEqual(h["vaddr"], 0x1006BC)
        self.assertEqual(h["paddr"], 0x6006BC)
        self.assertEqual((h["expected"], h["actual"], h["xor"]), (0xFF, 0x7D, 0x82))
        self.assertEqual(h["rereads"], [0x7D, 0x7D, 0x7D])
        self.assertEqual(h["after_rewrite"], 0xFF)
        self.assertEqual(buf[0x6BC], 0xFF)

    def test_end_to_end_smoke_with_log(self):
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "memtest.log")
            res = subprocess.run(
                [sys.executable, SCRIPT, "--mib", "16", "--chunk-mib", "4", "--workers", "2",
                 "--passes", "1", "--hold", "0.02", "--no-mlock", "--log", log],
                capture_output=True, text=True, check=True,
            )
            self.assertIn("bit_flip_events=0", res.stdout)
            self.assertIn("result=PASS", res.stdout)
            with open(log) as f:
                logged = f.read()
            self.assertEqual(logged, res.stdout)


if __name__ == "__main__":
    unittest.main()
