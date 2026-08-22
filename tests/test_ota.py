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
        ca_cert = kwargs.pop("ca_cert", b"test CA")
        super().__init__(
            "https://server.test/api/ota/dev",
            ca_cert=ca_cert,
            **kwargs
        )
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

    def test_duplicate_file_names_reject_whole_manifest(self):
        updater = FakeOTA(
            {"version": 1, "files": ["main.py", "main.py"]},
            {"main.py": b"new main"},
        )
        self.assertFalse(updater.check_and_update())
        self.assertEqual(self.state()["version"], 0)

    def test_activation_failure_restores_old_files_and_removes_new_files(self):
        ota._write_state({"version": 4, "boot_attempts": 0, "version_verified": 4})
        self.write("main.py", "old main")
        updater = FakeOTA(
            {"version": 8, "files": ["main.py", "extra.py"]},
            {"main.py": b"new main", "extra.py": b"new extra"},
        )

        original_rename = ota.os.rename

        def fail_extra_activation(source, destination):
            if source == "extra.py.new":
                raise OSError("simulated activation failure")
            return original_rename(source, destination)

        ota.os.rename = fail_extra_activation
        try:
            self.assertFalse(updater.check_and_update())
        finally:
            ota.os.rename = original_rename

        self.assertEqual(self.read("main.py"), "old main")
        self.assertFalse(os.path.exists("extra.py"))
        self.assertFalse(os.path.exists("extra.py.new"))
        self.assertEqual(self.state()["version"], 4)
        self.assertNotIn("update_in_progress", self.state())

    def test_backup_failure_aborts_without_replacing_current_file(self):
        ota._write_state({"version": 4, "boot_attempts": 0, "version_verified": 4})
        self.write("main.py", "old main")
        updater = FakeOTA(
            {"version": 8, "files": ["main.py"]},
            {"main.py": b"new main"},
        )

        original_rename = ota.os.rename

        def fail_backup(source, destination):
            if source == "main.py" and destination == "main.py.bak":
                raise OSError("simulated backup failure")
            return original_rename(source, destination)

        ota.os.rename = fail_backup
        try:
            self.assertFalse(updater.check_and_update())
        finally:
            ota.os.rename = original_rename

        self.assertEqual(self.read("main.py"), "old main")
        self.assertFalse(os.path.exists("main.py.new"))
        self.assertEqual(self.state()["version"], 4)


class TestTlsTransport(unittest.TestCase):
    def test_requires_https_and_a_trusted_ca(self):
        with self.assertRaisesRegex(ValueError, "must use https"):
            ota.OTA("http://server.test/api/ota", ca_cert=b"test CA")
        with self.assertRaisesRegex(ValueError, "CA certificate is required"):
            ota.OTA("https://server.test/api/ota", ca_cert=None)

    def test_refuses_network_access_until_clock_is_synchronized(self):
        updater = ota.OTA("https://server.test/api/ota", ca_cert=b"test CA")
        original_localtime = ota.time.localtime
        ota.time.localtime = lambda: (2021, 1, 1, 0, 0, 0, 0, 1)
        try:
            with self.assertRaisesRegex(OSError, "clock is not synchronized"):
                updater._http_get("https://server.test/api/ota/manifest")
        finally:
            ota.time.localtime = original_localtime

    def test_https_requires_ca_validation_and_hostname_checking(self):
        updater = ota.OTA("https://server.test/api/ota", ca_cert=b"trusted CA")

        class FakeSocket:
            def __init__(self):
                self.response = [
                    b"HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\n{}",
                    b"",
                ]
                self.connected = None
                self.closed = False

            def settimeout(self, _timeout):
                pass

            def connect(self, address):
                self.connected = address

            def write(self, _data):
                pass

            def read(self, _size):
                return self.response.pop(0)

            def close(self):
                self.closed = True

        fake_socket = FakeSocket()
        captured = {}
        original_getaddrinfo = ota.socket.getaddrinfo
        original_socket = ota.socket.socket
        original_wrap_socket = ota.ssl.wrap_socket
        ota.socket.getaddrinfo = lambda *_args: [(None, None, None, None, ("127.0.0.1", 443))]
        ota.socket.socket = lambda *_args: fake_socket

        def verified_wrap(sock, **kwargs):
            captured.update(kwargs)
            return sock

        ota.ssl.wrap_socket = verified_wrap
        try:
            self.assertEqual(
                updater._http_get("https://server.test/api/ota/manifest"),
                b"{}",
            )
        finally:
            ota.socket.getaddrinfo = original_getaddrinfo
            ota.socket.socket = original_socket
            ota.ssl.wrap_socket = original_wrap_socket

        self.assertEqual(captured["cert_reqs"], ota.ssl.CERT_REQUIRED)
        self.assertEqual(captured["cadata"], b"trusted CA")
        self.assertEqual(captured["server_hostname"], "server.test")
        self.assertTrue(fake_socket.closed)


class TestBootFlow(OtaTestCase):
    def test_state_read_recovers_interrupted_atomic_replace(self):
        old_state = {"version": 4, "boot_attempts": 0, "version_verified": 4}
        ota._write_state(old_state)
        os.rename("ota_state.json", "ota_state.json.prev")
        self.write("ota_state.json.tmp", '{"version": 8')

        self.assertEqual(ota.read_state(), old_state)

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

    def test_verified_boot_finishes_interrupted_backup_cleanup(self):
        ota._write_state({"version": 8, "boot_attempts": 0, "version_verified": 8})
        self.write("main.py", "good v8")
        self.write("main.py.bak", "old v4")

        ota.boot_check()
        self.assertEqual(self.read("main.py"), "good v8")
        self.assertFalse(os.path.exists("main.py.bak"))

    def test_boot_check_runs_rollback(self):
        ota._write_state({"version": 1, "boot_attempts": 2, "version_verified": 0})
        self.write("main.py", "broken")
        self.write("main.py.bak", "good")
        ota.boot_check()  # third unverified boot -> rollback
        self.assertEqual(self.read("main.py"), "good")

    def test_version_jump_rolls_back_to_exact_previous_version(self):
        ota._write_state({"version": 4, "boot_attempts": 0, "version_verified": 4})
        self.write("main.py", "v4 main")
        updater = FakeOTA(
            {"version": 8, "files": ["main.py", "extra.py"]},
            {"main.py": b"v8 main", "extra.py": b"v8 extra"},
        )
        with self.assertRaises(machine.ResetCalled):
            updater.check_and_update()

        self.assertEqual(self.state()["version"], 8)
        ota.rollback()
        self.assertEqual(self.state()["version"], 4)
        self.assertEqual(self.state()["version_verified"], 4)
        self.assertEqual(self.read("main.py"), "v4 main")
        self.assertFalse(os.path.exists("extra.py"))

    def test_boot_check_rolls_back_interrupted_activation_immediately(self):
        ota._write_state({
            "version": 4,
            "version_verified": 4,
            "boot_attempts": 0,
            "update_in_progress": True,
            "previous_version": 4,
            "previous_version_verified": 4,
            "update_files": ["main.py", "extra.py"],
            "added_files": ["extra.py"],
        })
        self.write("main.py", "partial v8")
        self.write("main.py.bak", "good v4")
        self.write("extra.py", "new file")

        ota.boot_check()
        self.assertEqual(self.read("main.py"), "good v4")
        self.assertFalse(os.path.exists("extra.py"))
        self.assertEqual(self.state()["version"], 4)
        self.assertNotIn("update_in_progress", self.state())

    def test_verified_update_removes_files_retired_by_manifest(self):
        ota._write_state({
            "version": 4,
            "boot_attempts": 0,
            "version_verified": 4,
            "installed_files": ["main.py", "retired.py"],
        })
        self.write("main.py", "v4 main")
        self.write("retired.py", "old helper")
        updater = FakeOTA(
            {"version": 8, "files": ["main.py"]},
            {"main.py": b"v8 main"},
        )
        with self.assertRaises(machine.ResetCalled):
            updater.check_and_update()

        self.assertTrue(os.path.exists("retired.py"))
        ota.mark_boot_ok()
        self.assertFalse(os.path.exists("retired.py"))
        self.assertEqual(self.state()["installed_files"], ["main.py"])


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
