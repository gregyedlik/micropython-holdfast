# holdfast

Resilient WiFi + MQTT + OTA scaffolding for unattended MicroPython devices
(Raspberry Pi Pico W / Pico 2 W, ESP32, and friends).

A *holdfast* is the anchor that keeps kelp gripping its rock through every
storm. This library does the same for a microcontroller that has to sit in a
shed for years: keep its grip on the network, notice when it has silently
lost it, update itself remotely, and recover from its own bad updates.

## What it does

- **WiFi management** (`holdfast.net.WifiManager`) — async connect with a
  per-attempt timeout, and **radio power-cycling** after failed attempts.
  WiFi chips (the Pico W's CYW43, the ESP32 radio) can wedge in a state
  where retrying through the normal API never recovers; cycling
  `active(False)`/`active(True)` clears it without rebooting the board.
  An optional `pm=` argument sets the radio's power-management mode —
  pass `network.WLAN.PM_NONE` on mains-powered devices, where the default
  modem power-save is a common source of dropped packets.
- **MQTT link management** (`holdfast.mqtt.MqttLink`) — explicit reconnect
  with exponential backoff (2 s → 60 s), a keepalive ping task that detects
  dead connections early, an incoming-message pump with callback dispatch
  that survives reconnects, forced WiFi radio cycling after repeated upstream
  MQTT failures, and retained latest-value publishing (set a value to `None`
  to clear its retained topic). Built on `umqtt.simple` on
  purpose: `umqtt.robust`'s auto-reconnect blocks the event loop in an
  unbounded retry and fights any external connection manager.
- **Application-level liveness** (`holdfast.mqtt.AckHeartbeat`) — publish a
  heartbeat, require the *server* to ACK it. This catches what transport
  keepalive cannot: a half-open subscribe socket, or a broker that is up
  while the service behind it is down. Repeated ACK timeouts ask the MQTT
  manager to cycle the WiFi radio even if the driver still reports connected.
  Optionally, `reset_after_no_ack_s` hard-resets the device after a long
  end-to-end outage while the firmware is still alive and retrying. Essential
  for devices that *receive* commands.
- **JSON command dispatch** (`holdfast.commands.JsonCommandHandler`) —
  subscribe to one exact MQTT command topic, validate JSON commands with an
  `action`, dispatch them to registered handlers, suppress duplicate command
  IDs, and publish retained command status.
- **OTA updates with rollback** (`holdfast.ota`) — manifest-driven file
  updates over certificate-verified HTTPS, all-or-nothing download, `.bak` backups,
  and boot-attempt counting: if a new version fails to boot 3 times, the
  previous version is restored automatically. A version is only "verified"
  once your app calls `mark_boot_ok()` — wire that to your first successful
  publish or first ACKed heartbeat. `config.py` is never touched, so
  per-device settings survive every update.
- **Task supervision** (`holdfast.supervisor`) — a hardware watchdog only
  catches a wedged event loop. If one task dies while others keep feeding
  the WDT, the device stays up half-broken forever. `supervisor.run()`
  reboots the moment any task ends.

## Install

As a git submodule (recommended when you also serve the files via OTA):

```sh
git submodule add https://github.com/gregyedlik/micropython-holdfast.git holdfast
```

Or with mip, straight onto a device:

```sh
mpremote mip install github:gregyedlik/micropython-holdfast
```

Requires `umqtt.simple` (`mpremote mip install umqtt.simple`).

## Quick start

See [examples/minimal](examples/minimal) — a complete device that publishes
uptime, stays connected through outages, and self-updates. The shape of
every holdfast app:

```python
wifi = WifiManager(SSID, PASS, led=led, wdt=wdt)
link = MqttLink(HOST, PORT, client_id=..., topic_prefix=..., wdt=wdt)
updater = ota.OTA(OTA_BASE, ca_cert=OTA_CA_CERT, wdt=wdt)

supervisor.run([
    ("manager",   link.manager_task(wifi)),       # keeps WiFi + MQTT alive
    ("pump",      link.pump_task()),              # dispatches messages, feeds WDT
    ("keepalive", link.keepalive_task()),         # early dead-link detection
    ("app",       my_app_task()),                 # your logic
    ("ota",       updater.checker_task(21600, wifi=wifi)),
])
```

Plus a two-line `boot.py` for rollback protection
([examples/minimal/boot.py](examples/minimal/boot.py)).

**Watchdog rule:** at least one task that runs unconditionally must feed the
WDT — `pump_task()` does, every iteration. Long backoff sleeps elsewhere are
then safe.

## OTA server contract

### MicroPython

Any HTTPS server works. Pass a trusted root CA certificate in PEM or DER format
when constructing `OTA`; plain HTTP and unverified HTTPS are rejected. The
device clock must be synchronized for certificate validity checks. Expose two
endpoints under a base URL of your choice:

```
GET <base>/manifest        -> {"version": 7, "files": ["main.py", "holdfast/net.py", ...]}
GET <base>/files/<name>    -> raw file content
```

File names may use at most one subdirectory level; the device validates
names conservatively and always refuses `config.py`. Release flow: copy the
new files where the server serves them, bump `version` in the manifest —
every device installs it at its next check, reboots, and rolls back by
itself if the new code can't boot.

### Arduino / ESP32

The same repository also contains an additive Arduino library under
`arduino/src`. Existing MicroPython projects, imports, manifests, and git
submodules do not change. PlatformIO discovers the Arduino part through the
root `library.json` when this repository is added as a dependency or submodule.

```cpp
#include <HoldfastOTA.h>

holdfast::OtaUpdater updater({
  .baseUrl = "https://example.test/api/ota/device-1",
  .target = "my-esp32-target",
  .authToken = shortLivedToken,
  .rootCa = rootCa,
  .currentVersion = 7,
});

if (updater.checkAndUpdate() == holdfast::OtaResult::Installed) {
  holdfast::OtaUpdater::reboot();
}
```

Binary releases use a separate, explicit manifest shape so they cannot be
mistaken for MicroPython file releases:

```json
{
  "schema": 1,
  "version": 8,
  "target": "my-esp32-target",
  "firmware": {
    "file": "firmware.bin",
    "size": 987654,
    "sha256": "..."
  }
}
```

Build the directory served by those endpoints with the shared release tool:

```sh
python3 holdfast/tools/build_release.py binary \
  --binary firmware/.pio/build/esp32c3/firmware.bin \
  --output server/firmware/device-1 \
  --version 8 \
  --target my-esp32-target
```

On ESP32, Holdfast writes the complete image to the inactive OTA partition and
verifies its size and SHA-256 before activating it. Enable the ESP-IDF rollback
option and call `OtaUpdater::markBootOk()` only after the new firmware has
completed an application-level health check (for example, an ACKed MQTT
heartbeat). A bad image then rolls back without inventing a second boot scheme.

## Failure-handling philosophy

Stay up, keep retrying, never trust a connection you haven't proven, and
when in doubt, reboot — the device must be stateless enough that a reset is
always safe. Backoff prevents hammering infrastructure during long outages;
the watchdog catches hangs; the supervisor catches task death; OTA rollback
catches bad firmware; the ACK heartbeat catches everything in between.

## Related work

[peterhinch/micropython-mqtt](https://github.com/peterhinch/micropython-mqtt)
(`mqtt_as`) is a mature resilient MQTT driver and a good alternative if you
only need the MQTT layer. holdfast trades some of its sophistication for a
smaller surface plus the pieces around it: OTA with rollback, server-ACKed
liveness, and task supervision.

## License

MIT
