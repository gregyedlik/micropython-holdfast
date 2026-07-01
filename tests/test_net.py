import unittest

import stub_env
from stub_env import real_asyncio

import machine
from holdfast.net import WifiManager


class TestWifiManager(unittest.TestCase):
    def test_connect_success(self):
        wifi = WifiManager("ssid", "pass")
        self.assertTrue(real_asyncio.run(wifi.connect()))
        self.assertTrue(wifi.isconnected())
        self.assertEqual(wifi.ip(), "192.168.1.50")

    def test_timeout_cycles_radio(self):
        wifi = WifiManager("ssid", "pass", attempt_timeout_s=20)
        wifi.wlan.will_connect = False
        self.assertFalse(real_asyncio.run(wifi.connect()))
        self.assertGreaterEqual(wifi.wlan.cycles, 1)
        self.assertFalse(wifi.wlan.active(None))

    def test_ensure_reconnects_after_drop(self):
        wifi = WifiManager("ssid", "pass")
        real_asyncio.run(wifi.connect())
        wifi.wlan.disconnect()
        self.assertTrue(real_asyncio.run(wifi.ensure()))

    def test_ensure_noop_when_connected(self):
        wifi = WifiManager("ssid", "pass")
        real_asyncio.run(wifi.connect())
        cycles_before = wifi.wlan.cycles
        self.assertTrue(real_asyncio.run(wifi.ensure()))
        self.assertEqual(wifi.wlan.cycles, cycles_before)

    def test_ensure_survives_inactive_radio(self):
        # After a failed attempt the radio is left inactive; on ESP32
        # disconnect() then raises. ensure() must absorb that instead of
        # killing the manager task.
        wifi = WifiManager("ssid", "pass")
        wifi.wlan.will_connect = False
        self.assertFalse(real_asyncio.run(wifi.connect()))
        self.assertFalse(wifi.wlan.active(None))
        self.assertFalse(real_asyncio.run(wifi.ensure()))  # must not raise
        wifi.wlan.will_connect = True
        self.assertTrue(real_asyncio.run(wifi.ensure()))

    def test_pm_applied_on_activation(self):
        wifi = WifiManager("ssid", "pass", pm=0xA11140)
        real_asyncio.run(wifi.connect())
        self.assertEqual(wifi.wlan.pm, 0xA11140)

    def test_pm_default_untouched(self):
        wifi = WifiManager("ssid", "pass")
        real_asyncio.run(wifi.connect())
        self.assertIsNone(wifi.wlan.pm)

    def test_blink_without_pin_toggle(self):
        # The wait loop blinks via Pin.value(); must work on ports whose
        # Pin lacks toggle() (ESP32).
        led = machine.Pin(0, machine.Pin.OUT)
        del machine.Pin.toggle  # simulate the ESP32 Pin class
        try:
            wifi = WifiManager("ssid", "pass", led=led)
            wifi.wlan.will_connect = False
            self.assertFalse(real_asyncio.run(wifi.connect()))
            self.assertEqual(led.state, 0)  # off after a failed attempt
        finally:
            machine.Pin.toggle = lambda self: setattr(self, "state", self.state ^ 1)

    def test_reconnect_cycles_radio_then_connects(self):
        wifi = WifiManager("ssid", "pass")
        real_asyncio.run(wifi.connect())

        self.assertTrue(real_asyncio.run(wifi.reconnect("test reset")))
        self.assertTrue(wifi.isconnected())
        self.assertEqual(wifi.wlan.cycles, 1)


if __name__ == "__main__":
    unittest.main()
