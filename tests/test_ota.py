import json
import os
import shutil
import tempfile
import unittest

import stub_env

import machine

from holdfast import ota


class FakeOTA(ota.OTA):
    """OTA with a canned server instead of real HTTP."""

    def __init__(self, manifest, files, **kwargs):
        super().__init__("https://server.test/api/ota/dev", **kwargs)
        self.manifest = manifest
        self.files = files
        self.fail_on = None  # file name whose download should fail

    def fetch_manifest(self):
        return self.manifest

    def fetch_file(self, fname):
        if fname == self.fail_on:
            raise OSError("download failed")
        return self.files[fname]


class OtaTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.prev_cwd = os.getcwd()
        os.chdir(self.dir)

    def tearDown(self):
        os.chdir(self.prev_cwd)
        shutil.rmtree(self.dir)

    def write(self, path, content):
        if "/" in path:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)

    def read(self, path):
        with open(path) as f:
            return f.read()

    def state(self):
        return ota.read_state()


class TestUpdate(OtaTestCase):
    def test_install_with_subdir_backup_and_reboot(self):
        self.write("main.py", "old main")
        updater = FakeOTA(
            {"version": 1, "files": ["main.py", "holdfast/net.py"]},
            {"main.py": b"new main", "holdfast/net.py": b"new net"},
        )
        with self.assertRaises(machine.ResetCalled):
            updater.check_and_update()
        self.assertEqual(self.read("main.py"), "new main")
        self.assertEqual(self.read("main.py.bak"), "old main")
        self.assertEqual(self.read("holdfast/net.py"), "new net")
        self.assertEqual(self.state()["version"], 1)
        self.assertEqual(self.state()["boot_attempts"], 0)

    def test_up_to_date_does_nothing(self):
        updater = FakeOTA({"version": 0, "files": ["main.py"]}, {})
        self.assertFalse(updater.check_and_update())
        self.assertEqual(self.state()["version"], 0)

    def test_failed_download_leaves_no_trace(self):
        self.write("main.py", "old main")
        updater = FakeOTA(
            {"version": 1, "files": ["main.py", "extra.py"]},
            {"main.py": b"new main", "extra.py": b"x"},
        )
        updater.fail_on = "extra.py"
        self.assertFalse(updater.check_and_update())
        self.assertEqual(self.read("main.py"), "old main")
        self.assertEqual(sorted(os.listdir()), ["main.py"])
        self.assertEqual(self.state()["version"], 0)

    def test_config_py_never_touched(self):
        self.write("config.py", "secrets")
        fetched = []

        class Spy(FakeOTA):
            def fetch_file(self, fname):
                fetched.append(fname)
                return super().fetch_file(fname)

        updater = Spy(
            {"version": 1, "files": ["config.py", "main.py"]},
            {"main.py": b"new main"},
        )
        with self.assertRaises(machine.ResetCalled):
            updater.check_and_update()
        self.assertEqual(fetched, ["main.py"])
        self.assertEqual(self.read("config.py"), "secrets")

    def test_bad_file_names_reject_whole_manifest(self):
        for bad in ["../evil.py", "a/b/c.py", "/abs.py", ".hidden", "a b.py", ""]:
            updater = FakeOTA({"version": 1, "files": [bad]}, {})
            self.assertFalse(updater.check_and_update(), bad)
            self.assertEqual(self.state()["version"], 0, bad)


class TestBootFlow(OtaTestCase):
    def test_unverified_boots_count_then_rollback(self):
        # Fresh OTA install: version 1, never verified.
        ota._write_state({"version": 1, "boot_attempts": 0, "version_verified": 0})
        self.write("main.py", "broken new")
        self.write("main.py.bak", "good old")
        self.write("holdfast/net.py", "broken net")
        self.write("holdfast/net.py.bak", "good net")

        for attempt in (1, 2):
            self.assertEqual(ota.increment_boot_attempts(), attempt)
            self.assertFalse(ota.needs_rollback())
        self.assertEqual(ota.increment_boot_attempts(), 3)
        self.assertTrue(ota.needs_rollback())

        ota.rollback()
        self.assertEqual(self.read("main.py"), "good old")
        self.assertEqual(self.read("holdfast/net.py"), "good net")
        self.assertFalse(os.path.exists("main.py.bak"))
        self.assertEqual(self.state()["version"], 0)
        self.assertEqual(self.state()["boot_attempts"], 0)

    def test_mark_boot_ok_verifies_and_cleans_baks(self):
        ota._write_state({"version": 1, "boot_attempts": 1, "version_verified": 0})
        self.write("main.py.bak", "old")
        self.write("holdfast/net.py.bak", "old net")
        ota.mark_boot_ok()
        state = self.state()
        self.assertEqual(state["version_verified"], 1)
        self.assertEqual(state["boot_attempts"], 0)
        self.assertFalse(os.path.exists("main.py.bak"))
        self.assertFalse(os.path.exists("holdfast/net.py.bak"))

    def test_verified_firmware_boots_do_not_count(self):
        ota._write_state({"version": 1, "boot_attempts": 0, "version_verified": 1})
        for _ in range(10):
            self.assertEqual(ota.increment_boot_attempts(), 0)
        self.assertFalse(ota.needs_rollback())

    def test_boot_check_runs_rollback(self):
        ota._write_state({"version": 1, "boot_attempts": 2, "version_verified": 0})
        self.write("main.py", "broken")
        self.write("main.py.bak", "good")
        ota.boot_check()  # third unverified boot -> rollback
        self.assertEqual(self.read("main.py"), "good")


class TestValidFname(unittest.TestCase):
    def test_accepts(self):
        for name in ["main.py", "boot.py", "holdfast/net.py", "version.json",
                     "my-mod_2.py"]:
            self.assertTrue(ota.valid_fname(name), name)

    def test_rejects(self):
        for name in ["", "../x.py", "a/b/c.py", "/etc/passwd", ".hidden",
                     "a b.py", "x\\y.py", "dir/", "/x.py", "a/.y.py"]:
            self.assertFalse(ota.valid_fname(name), name)


if __name__ == "__main__":
    unittest.main()
