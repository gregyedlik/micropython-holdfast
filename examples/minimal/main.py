"""Minimal holdfast example: publish uptime every 10 s, stay connected
through WiFi/broker outages, self-update via OTA.

Files on the device: main.py, boot.py, config.py, holdfast/.
"""

import time

import machine

import config
from holdfast import ota, supervisor
from holdfast.mqtt import MqttLink
from holdfast.net import WifiManager

led = machine.Pin("LED", machine.Pin.OUT)
wdt = machine.WDT(timeout=8000)

wifi = WifiManager(config.WIFI_SSID, config.WIFI_PASS,
                   hostname=config.TOPIC_PREFIX.replace("/", "-"),
                   led=led, wdt=wdt)
link = MqttLink(config.MQTT_HOST, config.MQTT_PORT,
                client_id=config.TOPIC_PREFIX.replace("/", "-"),
                user=config.MQTT_USER, password=config.MQTT_PASS,
                topic_prefix=config.TOPIC_PREFIX, wdt=wdt)
updater = ota.OTA(config.OTA_BASE, ca_cert=config.OTA_CA_CERT, wdt=wdt)

link.set_meta("_version", ota.local_version())


async def app_task():
    import uasyncio as asyncio
    boot_ok_sent = False
    while True:
        await asyncio.sleep(10)
        wdt.feed()
        link.update({"uptime_s": time.ticks_ms() // 1000})
        if await link.publish_latest():
            if not boot_ok_sent:
                ota.mark_boot_ok()
                boot_ok_sent = True


supervisor.run([
    ("manager", link.manager_task(wifi)),
    ("pump", link.pump_task()),
    ("keepalive", link.keepalive_task()),
    ("app", app_task()),
    ("ota", updater.checker_task(config.OTA_CHECK_INTERVAL, wifi=wifi)),
])
