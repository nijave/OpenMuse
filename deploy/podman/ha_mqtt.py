#!/usr/bin/env python3
"""Home Assistant MQTT discovery bridge for the OpenMuse capture supervisor.

Publishes one retained JSON state document (all values at once) plus
retained discovery configs so Home Assistant shows the headband as a device
with entities for capture state, connectivity, battery, data flow and
recording. Publish-only: no commands.

Patterns borrowed from the homelab's ChiliPad bridge (chilipad_bridge.py):
paho-mqtt >= 2.0 client, retained availability with an "offline" LWT,
`homeassistant/<component>/<unique_id>/config` retained discovery payloads
sharing one `device` block, and discovery resend on `homeassistant/status`
"online". paho-mqtt is imported lazily so importing this module (and
dashboard.py) never requires it; a missing paho-mqtt is logged once and MQTT
is disabled.
"""
from __future__ import annotations

import json
import re
import time

# The status.json states from `OpenMuse stream --status-file`, plus
# "not-running" (published when the child process is dead) and "unknown"
# (published when status.json is missing/stale while the child is alive;
# the dashboard badge already knows this state).
STATES = [
    "connecting", "streaming", "stalled", "reconnecting",
    "stopped", "failed", "not-running", "unknown",
]

# Attributes attached to the state sensor via json_attributes; these cover
# the fields that are not worth their own entity (one entity per sensor type
# in `samples` would be too many).
ATTRIBUTES = ["samples", "record_file", "firmware", "device_name",
              "last_error", "connect_count", "failed_attempts"]

# One entry per discovered entity. `suffix` extends the device id to make the
# HA unique_id; `template` extracts the value from the shared JSON state
# topic. Components/device classes:
#   * capture state: `sensor` + `device_class: enum` -- a fixed set of strings
#     (options), so HA renders a nice enum instead of a plain string.
#   * headband connected: `binary_sensor` + `connectivity` -- a boolean
#     link-up signal; HA maps it to the standard connectivity icon and
#     "Connected/Disconnected" wording.
#   * battery: `sensor` + `device_class: battery` -- HA supplies the battery
#     icon and charging state styling; unit % + state_class measurement.
#   * durations: `sensor` + `device_class: duration` with unit "s" -- HA
#     converts to a human form ("2h 0m") automatically.
#   * EEG samples: plain `sensor` with `total_increasing` -- a monotone
#     counter since process start; no device class fits.
#   * recording size: `sensor` + `device_class: data_size`, unit B -- HA
#     converts to KB/MB/GB automatically. suggested_display_precision 0
#     avoids a useless ".0" on exact byte counts.
ENTITIES: list[dict] = [
    {
        "component": "sensor",
        "suffix": "state",
        "name": "Capture state",
        "device_class": "enum",
        "options": STATES,
        "value_template": "{{ value_json.state | default('unknown') }}",
    },
    {
        "component": "binary_sensor",
        "suffix": "connected",
        "name": "Headband connected",
        "device_class": "connectivity",
        # 'ON' if truthy; a missing key (older payloads) means disconnected.
        "value_template": "{{ 'ON' if value_json.connected else 'OFF' }}",
    },
    {
        "component": "sensor",
        "suffix": "battery",
        "name": "Battery",
        "device_class": "battery",
        "unit": "%",
        "state_class": "measurement",
        # HA's `default` filter replaces an *undefined* value, so an omitted
        # (null) battery_percent renders as "unknown" while 0 still shows.
        "template_key": "battery_percent",
        "value_template": "{{ value_json.battery_percent "
                          "| default('unknown') }}",
    },
    {
        "component": "sensor",
        "suffix": "battery_time_to_empty",
        "name": "Battery time to empty",
        "device_class": "duration",
        "unit": "s",
        "template_key": "battery_time_to_empty_s",
        "value_template": "{{ value_json.battery_time_to_empty_s "
                          "| default('unknown') }}",
    },
    {
        "component": "sensor",
        "suffix": "last_data_age",
        "name": "Seconds since last packet",
        "device_class": "duration",
        "unit": "s",
        "entity_category": "diagnostic",
        "template_key": "last_data_age_s",
        "value_template": "{{ value_json.last_data_age_s "
                          "| default('unknown') }}",
    },
    {
        "component": "sensor",
        "suffix": "eeg_samples",
        "name": "EEG samples",
        "unit": "samples",
        "state_class": "total_increasing",
        "entity_category": "diagnostic",
        "template_key": "eeg_samples",
        "value_template": "{{ value_json.eeg_samples | default('unknown') }}",
    },
    {
        "component": "sensor",
        "suffix": "record_bytes",
        "name": "Recording size",
        "device_class": "data_size",
        "unit": "B",
        "suggested_display_precision": 0,
        "entity_category": "diagnostic",
        "template_key": "record_bytes",
        "value_template": "{{ value_json.record_bytes "
                          "| default('unknown') }}",
    },
]


def log(msg: str) -> None:
    print(f"[ha_mqtt] {time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def sanitize_id(address: str) -> str:
    """'00:55:DA:12:34:56' -> '0055da123456' (MQTT-topic/HA-id safe)."""
    return re.sub(r"[^0-9a-z]", "", address.lower())


def default_client_factory():
    """Lazily import paho-mqtt and build a v2-API client. Called only by
    HaMqttPublisher.start(); a kept-aside reference lets tests substitute it."""
    import paho.mqtt.client as mqtt  # imported lazily on purpose

    return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                       client_id="openmuse-capture")


class HaMqttPublisher:
    """Publishes the supervisor status to Home Assistant via MQTT discovery.

    All public methods are safe to call from the supervisor's main loop and
    never raise: MQTT problems are logged and retried (paho's automatic
    reconnect handles the connection, publish() skips ticks while offline).
    """

    def __init__(self, host: str, port: int = 1883, user: str = "",
                 password: str = "", prefix: str = "openmuse",
                 discovery_prefix: str = "homeassistant", interval: int = 10,
                 address: str = "", client_factory=None):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.interval = max(1, interval)
        self.enabled = bool(host)
        self._client_factory = client_factory or default_client_factory
        self._clock = time.time  # injectable for tests
        self._client = None
        self._connected = False
        self._last_publish = 0.0
        self._last_state: str | None = None
        self._last_connected: bool | None = None
        self._last_payload: dict | None = None  # resent on (re)connect
        self._firmware: str | None = None
        self._published_firmware: str | None = None

        ident = sanitize_id(address)
        self.device_id = f"openmuse_{ident}"
        # "Muse S Athena 3456": last two MAC octets keep multi-headband
        # devices apart in HA's UI.
        self.device_name = f"Muse S Athena {ident[-4:]}" if ident \
            else "Muse S Athena"
        self._discovery_prefix = discovery_prefix
        self.state_topic = f"{prefix}/{self.device_id}/state"
        self.availability_topic = f"{prefix}/{self.device_id}/availability"
        self.ha_status_topic = f"{discovery_prefix}/status"
        self.device_block = {
            "identifiers": [self.device_id],
            "name": self.device_name,
            "manufacturer": "Interaxon",
            "model": "Muse S Athena",
        }

    # -- status reporting (for the dashboard page and /status.json) ---------

    def status_line(self) -> str:
        if not self.enabled:
            return "mqtt: disabled"
        where = f"{self.host}:{self.port}"
        if self._connected:
            return f"mqtt: connected to {where}"
        return f"mqtt: connecting to {where}"

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Connect asynchronously and start the paho network loop. Never
        raises; if paho-mqtt is missing MQTT is disabled with one log line."""
        if not self.enabled:
            return
        try:
            client = self._client_factory()
        except ImportError as exc:
            log(f"paho-mqtt is not installed ({exc}); running without MQTT "
                f"(set MUSE_MQTT_HOST only on images built with it)")
            self.enabled = False
            return
        except Exception as exc:  # noqa: BLE001 -- never take down the supervisor
            log(f"cannot create MQTT client: {exc}; running without MQTT")
            self.enabled = False
            return
        if self.user:
            client.username_pw_set(self.user, self.password or None)
        # LWT: if this process dies without disconnecting, the broker marks
        # the device unavailable with a retained "offline".
        client.will_set(self.availability_topic, "offline", qos=1, retain=True)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.reconnect_delay_set(min_delay=1, max_delay=60)
        client.connect(self.host, self.port, keepalive=60)
        client.loop_start()
        self._client = client

    def stop(self) -> None:
        """Clean shutdown: retained 'offline', then disconnect. Never raises."""
        if not self.enabled or self._client is None:
            return
        try:
            if self._connected:
                self._client.publish(self.availability_topic, "offline",
                                     qos=1, retain=True).wait_for_publish(2)
            self._client.disconnect()
            self._client.loop_stop()
        except Exception as exc:  # noqa: BLE001
            log(f"error during MQTT shutdown: {exc}")

    # -- publishing ----------------------------------------------------------

    def publish(self, status: dict | None, supervisor: dict) -> None:
        """Rate-limited state publication; called once per supervisor tick.

        Publishes immediately when `state` or `connected` changes, otherwise
        at most every `interval` seconds. While disconnected the latest
        payload is cached and sent on the next successful tick after
        reconnecting.
        """
        if not self.enabled:
            return
        try:
            self._publish_inner(status, supervisor)
        except Exception as exc:  # noqa: BLE001
            log(f"publish failed: {exc}")

    def _publish_inner(self, status: dict | None, supervisor: dict) -> None:
        payload = self._build_payload(status, supervisor)
        state = payload["state"]
        connected = payload["connected"]
        changed = state != self._last_state or connected != self._last_connected
        now = self._clock()
        if not changed and now - self._last_publish < self.interval:
            return
        if not self._connected:
            return  # cache stays in self._last_payload; resent after reconnect
        self._last_publish = now
        self._last_state = state
        self._last_connected = connected
        self._last_payload = payload
        self._client.publish(self.state_topic, json.dumps(payload),
                             qos=1, retain=True)
        # Firmware became known (or changed) after a publish: refresh the
        # retained discovery configs so the device block's sw_version matches.
        if self._firmware != self._published_firmware:
            self._publish_discovery()

    def _build_payload(self, status: dict | None, supervisor: dict) -> dict:
        """One JSON document with every value; HA value_templates pick
        individual keys out of it, and the state sensor also exposes the
        ATTRIBUTES keys as json_attributes from the same topic."""
        alive = bool(supervisor.get("child_alive"))
        # The child is dead: no meaningful state or connection left, no
        # matter what a stale status.json still says.
        state = status.get("state", "unknown") if status else "unknown"
        if not alive:
            state = "not-running"
        firmware = status.get("firmware") if status else None
        if firmware:
            self._firmware = firmware
        connected = bool(status.get("connected")) if status and alive else False
        payload: dict = {"state": state, "connected": connected}
        samples = status.get("samples") if status else None
        eeg = samples.get("EEG") if isinstance(samples, dict) else None
        numeric = {
            "battery_percent": status.get("battery_percent") if status else None,
            "battery_time_to_empty_s":
                status.get("battery_time_to_empty_s") if status else None,
            "last_data_age_s": status.get("last_data_age_s") if status else None,
            "eeg_samples": eeg,
            "record_bytes": status.get("record_bytes") if status else None,
        }
        for key, value in numeric.items():
            # Null numerics are left out so the templates' default('unknown')
            # kicks in (HA renders "unknown"); 0 is kept.
            if value is not None:
                payload[key] = value
        for key in ATTRIBUTES:
            value = status.get(key) if status else None
            if value is not None:
                payload[key] = value
        return payload

    # -- discovery -----------------------------------------------------------

    def _discovery_payload(self, entity: dict) -> dict:
        device = dict(self.device_block)
        if self._firmware:
            device["sw_version"] = self._firmware
        payload = {
            "name": entity["name"],
            "unique_id": f"{self.device_id}_{entity['suffix']}",
            "object_id": f"{self.device_id}_{entity['suffix']}",
            "device": device,
            "availability_topic": self.availability_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
            "state_topic": self.state_topic,
            "value_template": entity["value_template"],
            "qos": 1,
        }
        for key in ("device_class", "unit", "options", "state_class",
                    "entity_category", "suggested_display_precision"):
            if key in entity:
                payload_key = {
                    "unit": "unit_of_measurement",
                    "options": "options",
                }.get(key, key)
                payload[payload_key] = entity[key]
        if entity["component"] == "sensor" and entity["suffix"] == "state":
            payload["json_attributes_topic"] = self.state_topic
            payload["json_attributes"] = ATTRIBUTES
        return payload

    def _publish_discovery(self) -> None:
        for entity in ENTITIES:
            topic = (f"{self._discovery_prefix}/{entity['component']}/"
                     f"{self.device_id}_{entity['suffix']}/config")
            self._client.publish(
                topic, json.dumps(self._discovery_payload(entity)),
                qos=1, retain=True)

    # -- paho callbacks --------------------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if getattr(reason_code, "is_failure", False):
            log(f"MQTT connect to {self.host}:{self.port} failed: {reason_code}")
            return
        log(f"mqtt: connected to {self.host}:{self.port}")
        self._connected = True
        client.subscribe([(self.ha_status_topic, 1)])
        client.publish(self.availability_topic, "online", qos=1, retain=True)
        self._publish_discovery()
        if self._last_payload is not None:
            client.publish(self.state_topic, json.dumps(self._last_payload),
                           qos=1, retain=True)

    def _on_disconnect(self, client, userdata, flags, reason_code,
                       properties=None):
        self._connected = False
        log(f"MQTT disconnected: {reason_code}")

    def _on_message(self, client, userdata, msg):
        if msg.topic == self.ha_status_topic and msg.payload == b"online":
            # Home Assistant restarted and dropped the retained discovery
            # configs; resend them (availability/state are retained anyway).
            log("homeassistant/status online; resending discovery")
            self._publish_discovery()
            if self._connected:
                client.publish(self.availability_topic, "online", qos=1,
                               retain=True)
