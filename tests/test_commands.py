import json
import unittest

import stub_env

from holdfast.commands import JsonCommandHandler
from holdfast.mqtt import MqttLink


def make_link():
    return MqttLink("broker.test", 1883, "client1", topic_prefix="t/dev")


def published_statuses(link):
    return [
        json.loads(payload)
        for topic, payload, retain, qos in link._client.published
        if topic == b"cmd/status"
    ]


class TestJsonCommandHandler(unittest.TestCase):
    def test_dispatches_registered_action_and_publishes_status(self):
        link = make_link()
        seen = []
        commands = JsonCommandHandler(link, b"cmd/in", b"cmd/status")
        commands.register("do.thing", lambda command: seen.append(command["id"]))
        link.connect()

        link._client.inbox.append((
            b"cmd/in",
            json.dumps({"id": "abc", "action": "do.thing"}).encode(),
        ))
        link._client.check_msg()

        self.assertEqual(seen, ["abc"])
        statuses = published_statuses(link)
        self.assertEqual([s["status"] for s in statuses], ["started", "finished"])
        self.assertEqual(statuses[0]["id"], "abc")
        self.assertEqual(statuses[0]["action"], "do.thing")

    def test_rejects_bad_json_and_unknown_action(self):
        link = make_link()
        commands = JsonCommandHandler(link, b"cmd/in", b"cmd/status")
        link.connect()

        link._client.inbox.append((b"cmd/in", b"not json"))
        link._client.inbox.append((
            b"cmd/in",
            json.dumps({"id": "abc", "action": "missing"}).encode(),
        ))
        link._client.check_msg()
        link._client.check_msg()

        statuses = published_statuses(link)
        self.assertEqual([s["status"] for s in statuses], ["rejected", "rejected"])
        self.assertIn("message", statuses[0])
        self.assertEqual(statuses[1]["message"], "unknown action")

    def test_duplicate_id_is_not_dispatched_twice(self):
        link = make_link()
        seen = []
        commands = JsonCommandHandler(link, b"cmd/in", b"cmd/status")
        commands.register("do.thing", lambda command: seen.append(command["id"]))
        link.connect()

        payload = json.dumps({"id": "abc", "action": "do.thing"}).encode()
        link._client.inbox.append((b"cmd/in", payload))
        link._client.inbox.append((b"cmd/in", payload))
        link._client.check_msg()
        link._client.check_msg()

        self.assertEqual(seen, ["abc"])
        statuses = published_statuses(link)
        self.assertEqual([s["status"] for s in statuses],
                         ["started", "finished", "duplicate"])

    def test_handler_failure_publishes_failed(self):
        link = make_link()
        commands = JsonCommandHandler(link, b"cmd/in", b"cmd/status")

        def fail(command):
            raise ValueError("boom")

        commands.register("do.thing", fail)
        link.connect()

        link._client.inbox.append((
            b"cmd/in",
            json.dumps({"id": "abc", "action": "do.thing"}).encode(),
        ))
        link._client.check_msg()

        statuses = published_statuses(link)
        self.assertEqual([s["status"] for s in statuses], ["started", "failed"])
        self.assertEqual(statuses[-1]["message"], "boom")


if __name__ == "__main__":
    unittest.main()
