"""Small JSON command dispatcher for MQTT-controlled devices.

Commands are received on one exact MQTT topic as JSON:
    {"id": "unique-command-id", "action": "ota.update_now", ...}

Handlers are ordinary synchronous callables. The dispatcher publishes simple
status messages before and after running a handler when a status topic is
configured.
"""

import json
import time


class JsonCommandHandler:
    """Dispatch JSON commands from MQTT to registered action handlers."""

    def __init__(self, link, command_topic, status_topic=None, wdt=None):
        self._link = link
        self._status_topic = status_topic
        self._wdt = wdt
        self._handlers = {}
        self._last_id = None
        link.subscribe(command_topic, self._on_message)

    def _feed(self):
        if self._wdt:
            self._wdt.feed()

    def register(self, action, handler):
        """Register handler(command) for an action string."""
        self._handlers[action] = handler

    def publish_status(self, command, status, message=None, extra=None):
        """Publish command status. Returns True when sent or no topic exists."""
        if not self._status_topic:
            return True

        payload = {
            "id": command.get("id"),
            "action": command.get("action"),
            "status": status,
            "uptime_ms": time.ticks_ms(),
        }
        if message:
            payload["message"] = str(message)
        if extra:
            payload.update(extra)
        return self._link.publish(
            self._status_topic,
            json.dumps(payload),
            retain=True,
            qos=0,
        )

    def _decode(self, msg):
        if isinstance(msg, bytes):
            msg = msg.decode()
        command = json.loads(msg)
        if not isinstance(command, dict):
            raise ValueError("command must be a JSON object")
        action = command.get("action")
        if not action:
            raise ValueError("command.action is required")
        return command

    def _on_message(self, topic, msg):
        self._feed()
        try:
            command = self._decode(msg)
        except Exception as exc:
            self.publish_status({"id": None, "action": None}, "rejected", exc)
            return

        command_id = command.get("id")
        action = command.get("action")
        if command_id and command_id == self._last_id:
            self.publish_status(command, "duplicate", "command already handled")
            return

        handler = self._handlers.get(action)
        if handler is None:
            self.publish_status(command, "rejected", "unknown action")
            return

        self._last_id = command_id
        self.publish_status(command, "started")
        self._feed()

        try:
            result = handler(command)
        except Exception as exc:
            self.publish_status(command, "failed", exc)
            return

        message = None
        if result is False:
            message = "no action taken"
        elif result not in (None, True):
            message = result
        self.publish_status(command, "finished", message)
