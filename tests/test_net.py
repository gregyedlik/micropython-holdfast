import unittest

import stub_env
from stub_env import real_asyncio

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


if __name__ == "__main__":
    unittest.main()
