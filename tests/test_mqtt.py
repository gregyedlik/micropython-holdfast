import json
import unittest

import stub_env
from stub_env import clock, spin, real_asyncio

from holdfast.mqtt import MqttLink, AckHeartbeat
from holdfast.net import WifiManager


def make_link(**kwargs):
    return MqttLink("broker.test", 1883, "client1", topic_prefix="t/dev", **kwargs)


class TestMqttLink(unittest.TestCase):
    def test_backoff_progression_and_cap(self):
        link = make_link(backoff_min_s=2, backoff_max_s=60)
        link._client.fail_connect = True

        async def scenario():
            observed = []
            for _ in range(7):
                ok = await link.ensure()
                self.assertFalse(ok)
                observed.append(link.backoff_s)
            return observed

        observed = real_asyncio.run(scenario())
        self.assertEqual(observed, [4, 8, 16, 32, 60, 60, 60])
        self.assertFalse(link.connected)

    def test_backoff_resets_after_success(self):
        link = make_link()
        link._client.fail_connect = True

        async def scenario():
            await link.ensure()
            await link.ensure()
            self.assertEqual(link.backoff_s, 8)
            link._client.fail_connect = False
            self.assertTrue(await link.ensure())

        real_asyncio.run(scenario())
        self.assertTrue(link.connected)
        self.assertEqual(link.backoff_s, 2)

    def test_subscriptions_survive_reconnect(self):
        link = make_link()
        link.subscribe(b"cmd/x", lambda t, m: None)
        link.connect()
        self.assertEqual(link._client.subscribed, [b"cmd/x"])
        # Simulate a drop and reconnect.
        link.connected = False
        real_asyncio.run(link.ensure())
        self.assertEqual(link._client.subscribed, [b"cmd/x", b"cmd/x"])

    def test_subscribe_failure_during_connect_keeps_link_dead(self):
        link = make_link()
        link.subscribe(b"cmd/x", lambda t, m: None)
        link._client.fail_subscribe = True

        async def scenario():
            return await link.ensure()

        self.assertFalse(real_asyncio.run(scenario()))
        self.assertFalse(link.connected)

    def test_dispatch_routes_to_callback(self):
        link = make_link()
        seen = []
        link.subscribe("cmd/x", lambda t, m: seen.append((t, m)))
        link.connect()
        link._client.inbox.append((b"cmd/x", b"payload"))
        link._client.inbox.append((b"cmd/other", b"ignored"))
        link._client.check_msg()
        link._client.check_msg()
        self.assertEqual(seen, [(b"cmd/x", b"payload")])

    def test_handler_exception_does_not_kill_dispatch(self):
        link = make_link()

        def bad_handler(t, m):
            raise ValueError("boom")

        link.subscribe(b"cmd/x", bad_handler)
        link.connect()
        link._client.inbox.append((b"cmd/x", b"payload"))
        link._client.check_msg()  # must not raise
        self.assertTrue(link.connected)

    def test_publish_failure_marks_link_dead(self):
        link = make_link()
        link.connect()
        link._client.fail_publish = True
        self.assertFalse(link.publish(b"a", b"b"))
        self.assertFalse(link.connected)

    def test_publish_latest_retained_and_clearing(self):
        link = make_link()
        link.connect()
        link.update({"temp": 21, "mode": "auto"})
        link.set_meta("_version", 3)
        ok = real_asyncio.run(link.publish_latest())
        self.assertTrue(ok)
        published = {t: (p, r, q) for t, p, r, q in link._client.published}
        self.assertEqual(published["t/dev/temp"], ("21", True, 1))
        self.assertEqual(published["t/dev/mode"], ("auto", True, 1))
        self.assertEqual(published["t/dev/_version"], ("3", True, 1))

        # None clears the retained topic exactly once.
        link._client.published.clear()
        link.update({"temp": None})
        real_asyncio.run(link.publish_latest())
        topics = [t for t, p, r, q in link._client.published]
        self.assertIn("t/dev/temp", topics)
        cleared = [p for t, p, r, q in link._client.published if t == "t/dev/temp"]
        self.assertEqual(cleared, [b""])
        link._client.published.clear()
        real_asyncio.run(link.publish_latest())
        topics = [t for t, p, r, q in link._client.published]
        self.assertNotIn("t/dev/temp", topics)

    def test_manager_cycles_wifi_after_repeated_mqtt_failures(self):
        wifi = WifiManager("ssid", "pass")
        link = make_link()
        link._client.fail_connect = True

        async def scenario():
            self.assertTrue(await wifi.connect())
            cycles_before = wifi.wlan.cycles
            task = real_asyncio.ensure_future(
                link.manager_task(
                    wifi,
                    interval_s=0,
                    wifi_cycle_after_mqtt_failures=2,
                )
            )
            try:
                for _ in range(30):
                    if wifi.wlan.cycles > cycles_before:
                        return
                    await spin(1)
                self.fail("manager_task did not cycle WiFi")
            finally:
                task.cancel()

        real_asyncio.run(scenario())
        self.assertGreaterEqual(wifi.wlan.cycles, 1)

    def test_manager_honors_wifi_cycle_request(self):
        wifi = WifiManager("ssid", "pass")
        link = make_link()

        async def scenario():
            self.assertTrue(await wifi.connect())
            cycles_before = wifi.wlan.cycles
            link.request_wifi_cycle("test request")
            task = real_asyncio.ensure_future(link.manager_task(wifi, interval_s=0))
            try:
                for _ in range(10):
                    if wifi.wlan.cycles > cycles_before:
                        return
                    await spin(1)
                self.fail("manager_task did not honor WiFi cycle request")
            finally:
                task.cancel()

        real_asyncio.run(scenario())
        self.assertGreaterEqual(wifi.wlan.cycles, 1)


class TestAckHeartbeat(unittest.TestCase):
    def make(self, link, **kwargs):
        firsts = []
        settings = {
            "interval_s": 10,
            "timeout_s": 25,
            "on_first_ack": lambda: firsts.append(1),
        }
        settings.update(kwargs)
        hb = AckHeartbeat(
            link,
            b"hb",
            b"hb/ack",
            lambda seq: json.dumps({"seq": seq}),
            **settings
        )
        return hb, firsts

    def heartbeats_sent(self, link):
        return [p for t, p, r, q in link._client.published if t == b"hb"]

    def test_heartbeat_sent_and_acked(self):
        link = make_link()
        hb, firsts = self.make(link)
        link.connect()

        async def scenario():
            task = real_asyncio.ensure_future(hb.run())
            await spin(5)
            self.assertEqual(len(self.heartbeats_sent(link)), 1)
            # While awaiting the ACK, no further heartbeat goes out.
            await spin(5)
            self.assertEqual(len(self.heartbeats_sent(link)), 1)
            # Deliver the ACK.
            link._client.inbox.append((b"hb/ack", json.dumps({"seq": 1}).encode()))
            link._client.check_msg()
            await spin(2)
            task.cancel()

        real_asyncio.run(scenario())
        self.assertEqual(firsts, [1])
        self.assertIsNone(hb._awaiting_seq)

    def test_on_first_ack_fires_only_once(self):
        link = make_link()
        hb, firsts = self.make(link)
        link.connect()

        async def scenario():
            task = real_asyncio.ensure_future(hb.run())
            for expected_seq in (1, 2):
                # Let the heartbeat go out, then ACK it.
                while hb._awaiting_seq is None:
                    await spin(1)
                link._client.inbox.append(
                    (b"hb/ack", json.dumps({"seq": expected_seq}).encode()))
                link._client.check_msg()
                await spin(2)
            task.cancel()

        real_asyncio.run(scenario())
        self.assertEqual(firsts, [1])

    def test_ack_timeout_marks_link_dead(self):
        link = make_link()
        hb, firsts = self.make(link)
        link.connect()

        async def scenario():
            task = real_asyncio.ensure_future(hb.run())
            # No ACK ever arrives; each loop iteration advances the fake
            # clock by 500 ms, so 60 iterations comfortably exceed the
            # 25 s timeout.
            await spin(60)
            task.cancel()

        real_asyncio.run(scenario())
        self.assertFalse(link.connected)
        self.assertEqual(firsts, [])
        self.assertIsNone(hb._awaiting_seq)

    def test_repeated_ack_timeouts_request_wifi_cycle(self):
        link = make_link()
        hb, firsts = self.make(link, interval_s=1, timeout_s=1,
                               wifi_cycle_after_timeouts=2)
        link.connect()

        async def scenario():
            task = real_asyncio.ensure_future(hb.run())
            try:
                while link.connected:
                    await spin(1)
                self.assertIsNone(link._consume_wifi_cycle_request())

                link.connect()
                while link.connected:
                    await spin(1)
                reason = link._consume_wifi_cycle_request()
                self.assertIn("heartbeat ACK timeouts", reason)
            finally:
                task.cancel()

        real_asyncio.run(scenario())
        self.assertEqual(firsts, [])

    def test_no_ack_reset_policy_calls_machine_reset(self):
        link = make_link()
        hb, firsts = self.make(link, interval_s=1, timeout_s=1,
                               reset_after_no_ack_s=5)
        link.connect()

        async def scenario():
            task = real_asyncio.ensure_future(hb.run())
            with self.assertRaises(stub_env.ResetCalled):
                await task

        real_asyncio.run(scenario())
        self.assertEqual(firsts, [])

    def test_ack_resets_no_ack_reset_timer(self):
        link = make_link()
        hb, firsts = self.make(link, interval_s=1, timeout_s=4,
                               reset_after_no_ack_s=5)
        link.connect()

        async def scenario():
            task = real_asyncio.ensure_future(hb.run())
            try:
                while hb._awaiting_seq is None:
                    await spin(1)
                link._client.inbox.append(
                    (b"hb/ack", json.dumps({"seq": 1}).encode()))
                link._client.check_msg()
                await spin(6)
                self.assertFalse(task.done())
            finally:
                task.cancel()

        real_asyncio.run(scenario())
        self.assertEqual(firsts, [1])

    def test_reconnect_clears_inflight_heartbeat(self):
        link = make_link()
        hb, firsts = self.make(link)
        link.connect()
        hb._awaiting_seq = 7
        # A reconnect runs the on_connect callbacks, which must reset the
        # in-flight heartbeat so the next one goes out immediately.
        link.connected = False
        real_asyncio.run(link.ensure())
        self.assertIsNone(hb._awaiting_seq)

    def test_stale_ack_ignored(self):
        link = make_link()
        hb, firsts = self.make(link)
        link.connect()
        hb._awaiting_seq = 2
        hb._on_ack(b"hb/ack", json.dumps({"seq": 1}).encode())
        self.assertEqual(hb._awaiting_seq, 2)
        self.assertEqual(firsts, [])


if __name__ == "__main__":
    unittest.main()
