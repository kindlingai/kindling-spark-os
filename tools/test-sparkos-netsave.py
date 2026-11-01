#!/usr/bin/env python3
"""sparkos-netsave against scratch directories: python3 tools/test-sparkos-netsave.py"""
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NETSAVE = os.path.join(HERE, "../overlay/usr/local/sbin/sparkos-netsave")
NP = "etc/netplan"
NM = "etc/NetworkManager/system-connections"


class Netsave(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.t)
        self.live, self.host, self.base = (os.path.join(self.t, n) for n in ("live", "host", "base"))
        # Boot: the disk has one profile, the image ships one file, and both are live and in the boot copy.
        self.put(self.host, NP, "00-installer-config.yaml", "network: {version: 2}\n")
        for root in (self.live, self.base):
            self.put(root, NP, "00-installer-config.yaml", "network: {version: 2}\n")
            self.put(root, NP, "01-image.yaml", "image\n")
            os.makedirs(os.path.join(root, NM))

    def put(self, root, d, name, text):
        os.makedirs(os.path.join(root, d), exist_ok=True)
        with open(os.path.join(root, d, name), "w") as f:
            f.write(text)

    def read(self, root, d, name):
        p = os.path.join(root, d, name)
        if not os.path.exists(p):
            return None
        with open(p) as f:
            return f.read()

    def run_netsave(self):
        env = dict(os.environ, SPARKOS_ROOT=self.live, SPARKOS_HOST=self.host, SPARKOS_BASE=self.base)
        return subprocess.run(["sh", NETSAVE], env=env, check=True, capture_output=True, text=True).stdout

    def test_saves_a_new_profile_and_a_name_with_spaces(self):
        self.put(self.live, NP, "90-NM-1234.yaml", "nm\n")
        self.put(self.live, NM, "Wired connection 1.nmconnection", "[connection]\n")
        out = self.run_netsave()
        self.assertEqual(self.read(self.host, NP, "90-NM-1234.yaml"), "nm\n")
        self.assertEqual(self.read(self.host, NM, "Wired connection 1.nmconnection"), "[connection]\n")
        self.assertIn("saved /etc/netplan/90-NM-1234.yaml", out)
        self.assertEqual(self.run_netsave(), "")                              # nothing left to do
        self.assertEqual([f for f in os.listdir(os.path.join(self.host, NP)) if f.startswith(".")], [])

    def test_saves_an_edit_and_a_delete(self):
        self.put(self.live, NP, "00-installer-config.yaml", "edited\n")
        self.run_netsave()
        self.assertEqual(self.read(self.host, NP, "00-installer-config.yaml"), "edited\n")
        os.remove(os.path.join(self.live, NP, "00-installer-config.yaml"))
        self.assertIn("removed /etc/netplan/00-installer-config.yaml", self.run_netsave())
        self.assertIsNone(self.read(self.host, NP, "00-installer-config.yaml"))

    def test_leaves_the_image_files_and_edits_made_on_the_disk(self):
        self.put(self.host, NP, "00-installer-config.yaml", "edited on the disk\n")
        self.put(self.host, NP, "50-added-on-disk.yaml", "disk\n")
        self.put(self.live, NP, "90-NM-1234.yaml", "nm\n")                    # an unrelated live edit
        self.run_netsave()
        self.assertEqual(self.read(self.host, NP, "00-installer-config.yaml"), "edited on the disk\n")
        self.assertEqual(self.read(self.host, NP, "50-added-on-disk.yaml"), "disk\n")
        self.assertIsNone(self.read(self.host, NP, "01-image.yaml"))

    def test_does_nothing_without_a_boot_copy(self):
        shutil.rmtree(self.base)
        self.put(self.live, NP, "90-NM-1234.yaml", "nm\n")
        self.run_netsave()
        self.assertIsNone(self.read(self.host, NP, "90-NM-1234.yaml"))


if __name__ == "__main__":
    unittest.main()
