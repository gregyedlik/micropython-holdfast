"""CPython stubs for MicroPython-only modules, so the holdfast logic can be
tested on a desktop. Import this module BEFORE importing anything from
holdfast.

Provides a fake millisecond clock: every (u)asyncio sleep and time.sleep
advances it instantly, so timeout/backoff tests run in real microseconds.
"""

import asyncio as real_asyncio
import os
import sys
import time as real_time
import types

# Make the repo root importable (for `import holdfast...`).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

clock = {"ms": 0}


async def spin(n=10):
    """Yield to the event loop n times without advancing the clock."""
    for _ in range(n):
        await real_asyncio.sleep(0)


# --- time ------------------------------------------------------------------

_time_stub = types.ModuleType("time")
for _n in dir(real_time):
    if not _n.startswith("_"):
        setattr(_time_stub, _n, getattr(real_time, _n))
_time_stub.ticks_ms = lambda: clock["ms"]
_time_stub.ticks_add = lambda a, b: a + b
_time_stub.ticks_diff = lambda a, b: a - b
_time_stub.sleep = lambda s: clock.__setitem__("ms", clock["ms"] + int(s * 1000))
_time_stub.sleep_ms = lambda ms: clock.__setitem__("ms", clock["ms"] + ms)
sys.modules["time"] = _time_stub

# --- uasyncio ----------------------------------------------------------------

_ua = types.ModuleType("uasyncio")


async def _sleep(s):
    clock["ms"] += int(s * 1000)
    await real_asyncio.sleep(0)


async def _sleep_ms(ms):
    clock["ms"] += int(ms)
    await real_asyncio.sleep(0)


_ua.sleep = _sleep
_ua.sleep_ms = _sleep_ms
_ua.Event = real_asyncio.Event
_ua.create_task = real_asyncio.create_task
_ua.run = real_asyncio.run
sys.modules["uasyncio"] = _ua

# --- machine -----------------------------------------------------------------


class ResetCalled(Exception):
    """Raised by the machine.reset stub so tests can observe reboots."""


_machine = types.ModuleType("machine")


class WDT:
    def __init__(self, timeout=0):
        self.timeout = timeout
        self.fed = 0

    def feed(self):
        self.fed += 1


class Pin:
    IN = 0
    OUT = 1

    def __init__(self, *args, **kwargs):
        self.state = 0

    def on(self):
        self.state = 1

    def off(self):
        self.state = 0

    def toggle(self):
        self.state ^= 1

    def init(self, *args, **kwargs):
        pass


def _reset():
    raise ResetCalled()


_machine.WDT = WDT
_machine.Pin = Pin
_machine.reset = _reset
_machine.ResetCalled = ResetCalled
_machine.unique_id = lambda: b"\x01\x02\x03\x04"
sys.modules["machine"] = _machine

# --- network -----------------------------------------------------------------

_network = types.ModuleType("network")


class WLAN:
    def __init__(self, iface=0):
        self._active = False
        self._connected = False
        self.will_connect = True  # set False to simulate an unreachable AP
        self.cycles = 0           # radio power-cycles observed

    def active(self, value=None):
        if value is None:
            return self._active
        if value is False and self._active:
            self.cycles += 1
        self._active = bool(value)
        if not self._active:
            self._connected = False

    def isconnected(self):
        return self._connected

    def connect(self, ssid, password):
        if self.will_connect:
            self._connected = True

    def disconnect(self):
        self._connected = False

    def ifconfig(self):
        return ("192.168.1.50", "255.255.255.0", "192.168.1.1", "8.8.8.8")


_network.WLAN = WLAN
_network.STA_IF = 0
_network.hostname = lambda *args: None
sys.modules["network"] = _network

# --- umqtt.simple --------------------------------------------------------------

_umqtt = types.ModuleType("umqtt")
_umqtt_simple = types.ModuleType("umqtt.simple")


class MQTTClient:
    def __init__(self, client_id, server, port=0, user=None, password=None,
                 keepalive=0):
        self.client_id = client_id
        self.server = server
        self.cb = None
        self.fail_connect = False
        self.fail_publish = False
        self.fail_subscribe = False
        self.is_connected = False
        self.connect_calls = 0
        self.published = []   # (topic, payload, retain, qos)
        self.subscribed = []
        self.inbox = []       # (topic, msg) pairs delivered by check_msg
        self.pings = 0

    def set_callback(self, f):
        self.cb = f

    def connect(self):
        self.connect_calls += 1
        if self.fail_connect:
            raise OSError("ECONNREFUSED")
        self.is_connected = True

    def disconnect(self):
        self.is_connected = False

    def subscribe(self, topic):
        if self.fail_subscribe:
            raise OSError("EPIPE")
        self.subscribed.append(topic)

    def publish(self, topic, payload, retain=False, qos=0):
        if self.fail_publish:
            raise OSError("EPIPE")
        self.published.append((topic, payload, retain, qos))

    def check_msg(self):
        if self.inbox:
            topic, msg = self.inbox.pop(0)
            self.cb(topic, msg)

    def ping(self):
        if self.fail_publish:
            raise OSError("EPIPE")
        self.pings += 1


_umqtt_simple.MQTTClient = MQTTClient
_umqtt.simple = _umqtt_simple
sys.modules["umqtt"] = _umqtt
sys.modules["umqtt.simple"] = _umqtt_simple
