"""OTA firmware updater with automatic rollback.

Update flow (OTA class):
  1. fetch <base_url>/manifest        -> {"version": N, "files": [...]}
  2. if N > local version, download each file from <base_url>/files/<name>
     to <name>.new (all-or-nothing)
  3. atomically-ish activate: rename current files to .bak, .new to current
  4. record the new version with boot_attempts=0 and reboot

Boot flow (module-level helpers, called from boot.py):
  - increment_boot_attempts() counts boots of an unverified version
  - needs_rollback() is True after MAX failed boots -> rollback() restores
    the .bak files
  - the application calls mark_boot_ok() once it has proven itself
    (e.g. first successful publish or first ACKed heartbeat), which
    verifies the version and removes the .bak files

Manifest file names may use one subdirectory level (e.g. "holdfast/net.py");
the directory is created on the device as needed. "config.py" is never
touched — per-device configuration survives every update.

No external dependencies — raw socket + ssl for HTTPS, HTTP/1.0 so the
server closes the connection and no chunked encoding is involved. A trusted
CA certificate is required and the device clock must be synchronized before
any network request is made.
"""

import os
import json
import socket
import ssl
import machine
import time

_STATE_FILE = "ota_state.json"
_STATE_TEMP_FILE = "ota_state.json.tmp"
_STATE_PREVIOUS_FILE = "ota_state.json.prev"
_MAX_BOOT_ATTEMPTS = 3
_MIN_TLS_YEAR = 2024


# ---------------------------------------------------------------------------
# State persistence (module-level: boot.py needs these without any config)
# ---------------------------------------------------------------------------

def read_state():
    """Load OTA state from flash. Returns defaults if missing."""
    for filename in (_STATE_FILE, _STATE_PREVIOUS_FILE):
        try:
            with open(filename, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"version": 0, "boot_attempts": 0, "version_verified": -1}


def _write_state(state):
    with open(_STATE_TEMP_FILE, "w") as f:
        json.dump(state, f)
        try:
            f.flush()
        except Exception:
            pass
    try:
        os.sync()
    except Exception:
        pass

    try:
        os.remove(_STATE_PREVIOUS_FILE)
    except OSError:
        pass
    try:
        os.rename(_STATE_FILE, _STATE_PREVIOUS_FILE)
    except OSError:
        pass
    try:
        os.rename(_STATE_TEMP_FILE, _STATE_FILE)
    except Exception:
        # Keep the previous valid state recoverable if the final rename fails.
        try:
            os.rename(_STATE_PREVIOUS_FILE, _STATE_FILE)
        except OSError:
            pass
        raise
    try:
        os.remove(_STATE_PREVIOUS_FILE)
    except OSError:
        pass


def local_version():
    return read_state().get("version", 0)


# ---------------------------------------------------------------------------
# File-name validation and filesystem helpers
# ---------------------------------------------------------------------------

def valid_fname(fname):
    """Allow "name.ext" or "dir/name.ext" — nothing deeper, no dotfiles,
    no traversal, conservative character set."""
    if not fname or "\\" in fname:
        return False
    parts = fname.split("/")
    if len(parts) > 2:
        return False
    for part in parts:
        if not part or part.startswith("."):
            return False
        for ch in part:
            if not (ch.isalpha() or ch.isdigit() or ch in "._-"):
                return False
    return True


def _ensure_parent_dir(fname):
    if "/" in fname:
        d = fname.split("/")[0]
        try:
            os.mkdir(d)
        except OSError:
            pass  # already exists


def _is_dir(path):
    try:
        return os.stat(path)[0] & 0x4000 != 0
    except OSError:
        return False


def _iter_bak_files():
    """Yield .bak file paths in the cwd (the flash root on a device,
    where boot.py runs) and one directory level deep."""
    for entry in os.listdir():
        if _is_dir(entry):
            for sub in os.listdir(entry):
                if sub.endswith(".bak"):
                    yield entry + "/" + sub
        elif entry.endswith(".bak"):
            yield entry


def _remove_bak_files():
    for bak in _iter_bak_files():
        try:
            os.remove(bak)
        except OSError:
            pass


def _path_exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _clear_update_transaction(state):
    for key in ("update_in_progress", "previous_version",
                "previous_version_verified", "update_files",
                "added_files", "obsolete_files"):
        state.pop(key, None)


# ---------------------------------------------------------------------------
# Boot-time helpers (rollback protection), called from boot.py
# ---------------------------------------------------------------------------

def increment_boot_attempts():
    """Count this boot if the current version has never proven itself.
    Verified versions always return 0 — their reboots are harmless."""
    state = read_state()
    current_ver = state.get("version", 0)
    verified_ver = state.get("version_verified", -1)

    if current_ver == verified_ver:
        if state.get("boot_attempts", 0) != 0:
            state["boot_attempts"] = 0
            _write_state(state)
        return 0

    state["boot_attempts"] = state.get("boot_attempts", 0) + 1
    _write_state(state)
    return state["boot_attempts"]


def needs_rollback():
    return read_state().get("boot_attempts", 0) >= _MAX_BOOT_ATTEMPTS


def rollback(wdt=None):
    """Restore .bak files and reset the OTA state."""
    print("[ota] ROLLBACK — restoring previous firmware")
    state = read_state()

    # Files first introduced by the failed version have no .bak to restore.
    # Remove only the exact, validated paths recorded before activation.
    for fname in state.get("added_files", []):
        if not valid_fname(fname):
            continue
        try:
            os.remove(fname)
            print("[ota]   removed new file %s" % fname)
        except OSError:
            pass
        if wdt:
            wdt.feed()

    for bak in _iter_bak_files():
        original = bak[:-4]
        try:
            try:
                os.remove(original)
            except OSError:
                pass
            os.rename(bak, original)
            print("[ota]   restored %s" % original)
            if wdt:
                wdt.feed()
        except Exception as exc:
            print("[ota]   failed to restore %s: %s" % (original, exc))

    for fname in state.get("update_files", []):
        if not valid_fname(fname):
            continue
        try:
            os.remove(fname + ".new")
        except OSError:
            pass

    previous_version = state.get("previous_version")
    if not isinstance(previous_version, int):
        # Compatibility with OTA state written before transaction metadata
        # existed. Old releases advanced one version at a time.
        previous_version = max(0, state.get("version", 1) - 1)
    state["version"] = previous_version
    previous_verified = state.get("previous_version_verified")
    if isinstance(previous_verified, int):
        state["version_verified"] = previous_verified
    state["boot_attempts"] = 0
    _clear_update_transaction(state)
    _write_state(state)
    print("[ota] rollback complete (now v%d)" % state["version"])


def mark_boot_ok():
    """Call once the application has proven itself (first successful
    publish, first ACKed heartbeat, ...). Verifies the current version
    so future reboots never trigger rollback, and removes .bak files."""
    state = read_state()
    current_ver = state.get("version", 0)
    if (state.get("boot_attempts", 0) == 0
            and state.get("version_verified", -1) == current_ver
            and not state.get("update_files")):
        return  # already verified

    update_files = state.get("update_files")
    if isinstance(update_files, list):
        state["installed_files"] = update_files
    for fname in state.get("obsolete_files", []):
        if not valid_fname(fname):
            continue
        try:
            os.remove(fname)
            print("[ota]   removed obsolete file %s" % fname)
        except OSError:
            pass

    state["boot_attempts"] = 0
    state["version_verified"] = current_ver
    _clear_update_transaction(state)
    _write_state(state)
    print("[ota] boot marked OK — v%d verified" % current_ver)
    _remove_bak_files()


def boot_check():
    """The entire boot.py duty: count the attempt, roll back if needed.
    Never raises — a broken OTA module must not brick the device."""
    try:
        if read_state().get("update_in_progress"):
            print("[boot] interrupted OTA activation — rolling back!")
            rollback()
            return
        attempts = increment_boot_attempts()
        if attempts == 0:
            print("[boot] verified firmware — clean boot")
            # A reset after the verified-state commit but before backup cleanup
            # is harmless; finish that cleanup on the next boot.
            _remove_bak_files()
        else:
            print("[boot] unverified firmware — attempt #%d" % attempts)
        if needs_rollback():
            print("[boot] too many failed boots — rolling back!")
            rollback()
    except Exception as exc:
        print("[boot] ota check skipped:", exc)


# ---------------------------------------------------------------------------
# Updater
# ---------------------------------------------------------------------------

class OTA:
    """Checks <base_url>/manifest and installs newer firmware.

    base_url example: "https://example.org/api/ota/mydevice"
    The server must expose:
        <base_url>/manifest        JSON {"version": N, "files": [names]}
        <base_url>/files/<name>    raw file content
    """

    def __init__(self, base_url, ca_cert, wdt=None):
        self._base_url = base_url.rstrip("/")
        if not self._base_url.startswith("https://"):
            raise ValueError("OTA base URL must use https")
        if not ca_cert:
            raise ValueError("OTA trusted CA certificate is required")
        self._ca_cert = ca_cert
        self._wdt = wdt

    def _feed(self):
        if self._wdt:
            self._wdt.feed()

    def clock_is_valid(self):
        """TLS certificate dates cannot be checked before NTP has set RTC."""
        try:
            return time.localtime()[0] >= _MIN_TLS_YEAR
        except Exception:
            return False

    # -- minimal HTTPS GET, HTTP/1.0, no chunked encoding -------------------

    def _http_get(self, url):
        if not url.startswith("https://"):
            raise ValueError("OTA requests require https")
        if not self.clock_is_valid():
            raise OSError("clock is not synchronized; TLS verification unavailable")

        rest = url[8:]
        port = 443

        slash = rest.find("/")
        if slash < 0:
            host, path = rest, "/"
        else:
            host, path = rest[:slash], rest[slash:]
        if ":" in host:
            host, port_s = host.rsplit(":", 1)
            port = int(port_s)

        self._feed()
        addr = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)[0][-1]
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(15)
        try:
            sock.connect(addr)
            sock = ssl.wrap_socket(
                sock,
                cert_reqs=ssl.CERT_REQUIRED,
                cadata=self._ca_cert,
                server_hostname=host,
            )
            self._feed()

            req = "GET %s HTTP/1.0\r\nHost: %s\r\nConnection: close\r\n\r\n" % (path, host)
            sock.write(req.encode())
            self._feed()

            header_buf = b""
            while b"\r\n\r\n" not in header_buf:
                chunk = sock.read(256)
                if not chunk:
                    break
                header_buf += chunk
                self._feed()

            header_end = header_buf.find(b"\r\n\r\n")
            if header_end < 0:
                raise OSError("bad HTTP response")

            headers = header_buf[:header_end].decode()
            status_line = headers.split("\r\n")[0]
            if " 200 " not in status_line:
                raise OSError("HTTP %s" % status_line)

            body = bytearray(header_buf[header_end + 4:])
            while True:
                chunk = sock.read(512)
                if not chunk:
                    break
                body.extend(chunk)
                self._feed()
            return bytes(body)
        finally:
            sock.close()

    def fetch_manifest(self):
        return json.loads(self._http_get(self._base_url + "/manifest"))

    def fetch_file(self, fname):
        return self._http_get(self._base_url + "/files/" + fname)

    # -- update -------------------------------------------------------------

    def check_and_update(self):
        """Install a newer firmware version if the manifest offers one.
        Reboots on success; returns False if up to date or on any error
        (errors never leave partial state behind)."""
        state = read_state()
        current = state.get("version", 0)
        print("[ota] checking for updates (current v%d)" % current)

        try:
            manifest = self.fetch_manifest()
        except Exception as exc:
            print("[ota] manifest fetch failed:", exc)
            return False

        remote = manifest.get("version", 0)
        files = [f for f in manifest.get("files", []) if f != "config.py"]

        if remote <= current:
            print("[ota] up to date (server v%d)" % remote)
            return False

        for fname in files:
            if not valid_fname(fname):
                print("[ota] rejecting manifest with bad file name: %r" % fname)
                return False
        if len(files) != len(set(files)):
            print("[ota] rejecting manifest with duplicate file names")
            return False

        print("[ota] update available: v%d -> v%d (%d files)"
              % (current, remote, len(files)))

        # Phase 1: download everything as .new — all or nothing.
        for fname in files:
            print("[ota] downloading %s" % fname)
            try:
                data = self.fetch_file(fname)
                _ensure_parent_dir(fname)
                with open(fname + ".new", "wb") as f:
                    f.write(data)
                print("[ota]   %d bytes OK" % len(data))
            except Exception as exc:
                print("[ota]   FAILED:", exc)
                self._cleanup_new(files)
                return False

        # Record enough information to undo a version jump, a newly introduced
        # file, or a power loss at any point during activation.
        installed_files = state.get("installed_files", [])
        if not isinstance(installed_files, list):
            installed_files = []
        state["update_in_progress"] = True
        state["previous_version"] = current
        state["previous_version_verified"] = state.get("version_verified", -1)
        state["update_files"] = files
        state["added_files"] = [f for f in files if not _path_exists(f)]
        state["obsolete_files"] = [
            f for f in installed_files
            if valid_fname(f) and f != "config.py" and f not in files
        ]
        _write_state(state)

        # Phase 2: back up current files and activate the new ones.
        print("[ota] activating update")
        added_files = state.get("added_files", [])
        for fname in files:
            try:
                if fname not in added_files:
                    os.rename(fname, fname + ".bak")
                os.rename(fname + ".new", fname)
            except Exception as exc:
                print("[ota] activation failed for %s: %s" % (fname, exc))
                self._undo_activation(files)
                return False

        # Phase 3: record the new (unverified) version and reboot.
        state["version"] = remote
        state["boot_attempts"] = 0
        state["update_in_progress"] = False
        _write_state(state)
        print("[ota] update complete — rebooting in 3s")
        time.sleep(3)
        machine.reset()
        return True  # unreachable, for clarity

    async def checker_task(self, interval_s, wifi=None, initial_delay_s=60):
        """Periodically check for updates (when the network is up)."""
        import uasyncio as asyncio
        await asyncio.sleep(initial_delay_s)
        while True:
            self._feed()
            network_ready = wifi is None or wifi.isconnected()
            if network_ready and self.clock_is_valid():
                try:
                    self.check_and_update()
                except Exception as exc:
                    print("[ota] check failed:", exc)
                await asyncio.sleep(interval_s)
            else:
                if network_ready:
                    print("[ota] waiting for synchronized clock")
                await asyncio.sleep(30)

    # -- helpers -------------------------------------------------------------

    def _cleanup_new(self, files):
        for fname in files:
            try:
                os.remove(fname + ".new")
            except OSError:
                pass

    def _undo_activation(self, files):
        rollback(self._wdt)
        self._cleanup_new(files)
