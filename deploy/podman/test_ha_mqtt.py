"""Unit tests for ha_mqtt.py (fake paho client injected; no broker)."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("ha_mqtt", HERE / "ha_mqtt.py")
ha_mqtt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ha_mqtt)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

ADDRESS = "00:55:DA:12:34:56"

STATUS_ALIVE = {
    "state": "streaming",
    "connected": True,
    "device_name": "Muse S Athena",
    "firmware": "1.2.3",
    "battery_percent": 80,
    "battery_time_to_empty_s": 7200,
    "samples": {"EEG": 1234, "ACCGYRO": 99, "OPTICS": 5},
    "last_data_age_s": 0.4,
    "connected_since": 1000.0,
    "process_started": 900.0,
    "connect_count": 2,
    "failed_attempts": 1,
    "last_error": None,
    "record_file": "/data/muse_2026-07-20_1200.txt",
    "record_bytes": 5 * 1024 * 1024,
}

SUPERVISOR_ALIVE = {
    "child_alive": True,
    "child_pid": 4242,
    "uptime_s": 120.0,
    "status_age_s": 1.0,
    "compress": "zstd",
    "compressing": [],
}


class Published:
    def __init__(self):
        self.waited = False

    def wait_for_publish(self, timeout=None):
        self.waited = True


class FakeClient:
    """Minimal paho-mqtt v2 client double: records everything."""

    instances: list["FakeClient"] = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.wills: list[tuple] = []
        self.published: list[tuple] = []  # (topic, payload, qos, retain)
        self.subscribed: list = []
        self.connected_host = None
        self.connected_port = None
        self.username_pw = None
        self.loop_started = False
        self.loop_stopped = False
        self.disconnected = False
        self.on_connect = None
        self.on_message = None
        self.on_disconnect = None
        FakeClient.instances.append(self)

    def username_pw_set(self, user, password=None):
        self.username_pw = (user, password)

    def will_set(self, topic, payload, qos=0, retain=False):
        self.wills.append((topic, payload, qos, retain))

    def reconnect_delay_set(self, min_delay=None, max_delay=None):
        pass

    def connect(self, host, port=1883, keepalive=60):
        self.connected_host = host
        self.connected_port = port

    def loop_start(self):
        self.loop_started = True

    def loop_stop(self):
        self.loop_stopped = True

    def disconnect(self):
        self.disconnected = True

    def subscribe(self, topics, qos=0):
        self.subscribed.append(topics)

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))
        return Published()

    # -- test helpers -------------------------------------------------------

    def fire_connect(self, failed=False):
        rc = SimpleNamespace(is_failure=failed, name="Success")
        self.on_connect(self, None, {}, rc, None)

    def fire_message(self, topic, payload):
        msg = SimpleNamespace(topic=topic, payload=payload)
        self.on_message(self, None, msg)


def make_publisher(**overrides) -> ha_mqtt.HaMqttPublisher:
    kwargs = dict(
        host="broker.local",
        port=1883,
        user="u",
        password="p",
        prefix="openmuse",
        discovery_prefix="homeassistant",
        interval=10,
        address=ADDRESS,
        client_factory=FakeClient,
    )
    kwargs.update(overrides)
    return ha_mqtt.HaMqttPublisher(**kwargs)


def publish_once(pub: ha_mqtt.HaMqttPublisher, status=None, sup=None) -> FakeClient:
    client = FakeClient.instances[-1]
    client.fire_connect()
    pub.publish(status if status is not None else STATUS_ALIVE,
                sup if sup is not None else SUPERVISOR_ALIVE)
    return client


class FakeClock:
    """Injectable clock: t() returns the current fake time."""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now


def device_blocks(client: FakeClient) -> set:
    # Discovery is published at connect and again when firmware becomes
    # known; only the latest payload per config topic is in force.
    latest: dict[str, dict] = {}
    for topic, payload, _, _ in client.published:
        if topic.endswith("/config"):
            latest[topic] = json.loads(payload)
    return {json.dumps(p["device"], sort_keys=True) for p in latest.values()}


def config_payloads(client: FakeClient) -> dict[str, dict]:
    return {
        topic: json.loads(payload)
        for topic, payload, _, _ in client.published
        if topic.endswith("/config")
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class DisabledTests(unittest.TestCase):
    def setUp(self):
        FakeClient.instances.clear()

    def test_empty_host_disables_everything(self):
        pub = make_publisher(host="")
        self.assertFalse(pub.enabled)
        pub.start()
        pub.publish(STATUS_ALIVE, SUPERVISOR_ALIVE)
        pub.stop()
        self.assertEqual(FakeClient.instances, [])
        self.assertEqual(pub.status_line(), "mqtt: disabled")


class PahoMissingTests(unittest.TestCase):
    def test_import_error_is_logged_once_and_disabled(self):
        def broken_factory(*args, **kwargs):
            raise ImportError("No module named 'paho'")
        pub = make_publisher(client_factory=broken_factory)
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            pub.start()  # must not raise
            pub.publish(STATUS_ALIVE, SUPERVISOR_ALIVE)  # no-op, no crash
            pub.publish(STATUS_ALIVE, SUPERVISOR_ALIVE)
            pub.stop()
        out = captured.getvalue()
        self.assertIn("paho-mqtt", out)
        self.assertEqual(out.count("paho-mqtt"), 1)  # logged once, not per tick
        self.assertFalse(pub.enabled)
        self.assertEqual(pub.status_line(), "mqtt: disabled")


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        FakeClient.instances.clear()
        self.pub = make_publisher()
        self.pub.start()
        self.client = publish_once(self.pub)

    def test_start_configures_lwt_and_connects(self):
        client = self.client
        self.assertEqual(client.connected_host, "broker.local")
        self.assertEqual(client.connected_port, 1883)
        self.assertTrue(client.loop_started)
        self.assertEqual(client.username_pw, ("u", "p"))
        topic, payload, qos, retain = client.wills[0]
        self.assertEqual(topic, "openmuse/openmuse_0055da123456/availability")
        self.assertEqual(payload, "offline")
        self.assertEqual(qos, 1)
        self.assertTrue(retain)

    def test_availability_online_on_connect(self):
        avail = [p for t, p, q, r in self.client.published
                 if t.endswith("/availability")]
        self.assertIn("online", avail)
        idx = [t for t, *_ in self.client.published].index(
            "openmuse/openmuse_0055da123456/availability")
        _, _, qos, retain = self.client.published[idx]
        self.assertEqual((qos, retain), (1, True))

    def test_discovery_sent_for_every_entity(self):
        topics = {t for t, *_ in self.client.published if t.endswith("/config")}
        components = {t.split("/")[1] for t in topics}
        self.assertEqual(components, {"sensor", "binary_sensor"})
        sensors = [t for t in topics if t.startswith("homeassistant/sensor/")]
        suffixes = {t.split("/")[2] for t in sensors}
        self.assertEqual(suffixes, {
            "openmuse_0055da123456_state",
            "openmuse_0055da123456_battery",
            "openmuse_0055da123456_battery_time_to_empty",
            "openmuse_0055da123456_last_data_age",
            "openmuse_0055da123456_eeg_samples",
            "openmuse_0055da123456_record_bytes",
        })
        self.assertIn("homeassistant/binary_sensor/"
                      "openmuse_0055da123456_connected/config", topics)
        for t in topics:
            self.assertIn("/config", t)
            self.assertTrue(t.startswith("homeassistant/"))

    def test_device_block_shared(self):
        blocks = device_blocks(self.client)
        self.assertEqual(len(blocks), 1)
        device = json.loads(next(iter(blocks)))
        self.assertEqual(device, {
            "identifiers": ["openmuse_0055da123456"],
            "name": "Muse S Athena 3456",
            "manufacturer": "Interaxon",
            "model": "Muse S Athena",
            "sw_version": "1.2.3",
        })

    def test_state_entity_template_and_attributes(self):
        cfg = config_payloads(self.client)[
            "homeassistant/sensor/openmuse_0055da123456_state/config"]
        self.assertEqual(cfg["device_class"], "enum")
        self.assertEqual(cfg["options"],
                         ["connecting", "streaming", "stalled", "reconnecting",
                          "stopped", "failed", "not-running", "unknown"])
        self.assertEqual(cfg["state_topic"],
                         "openmuse/openmuse_0055da123456/state")
        self.assertEqual(cfg["value_template"],
                         "{{ value_json.state | default('unknown') }}")
        self.assertEqual(
            cfg["json_attributes_topic"],
            "openmuse/openmuse_0055da123456/state")
        self.assertEqual(cfg["json_attributes"],
                         ["samples", "record_file", "firmware", "device_name",
                          "last_error", "connect_count", "failed_attempts"])

    def test_battery_entity(self):
        cfg = config_payloads(self.client)[
            "homeassistant/sensor/openmuse_0055da123456_battery/config"]
        self.assertEqual(cfg["device_class"], "battery")
        self.assertEqual(cfg["unit_of_measurement"], "%")
        self.assertEqual(cfg["state_class"], "measurement")
        self.assertEqual(cfg["value_template"],
                         "{{ value_json.battery_percent | default('unknown') }}")

    def test_duration_entities(self):
        cfgs = config_payloads(self.client)
        empty = cfgs[
            "homeassistant/sensor/openmuse_0055da123456_battery_time_to_empty/config"]
        self.assertEqual(empty["device_class"], "duration")
        self.assertEqual(empty["unit_of_measurement"], "s")
        self.assertEqual(empty["value_template"],
                         "{{ value_json.battery_time_to_empty_s "
                         "| default('unknown') }}")
        age = cfgs[
            "homeassistant/sensor/openmuse_0055da123456_last_data_age/config"]
        self.assertEqual(age["device_class"], "duration")
        self.assertEqual(age["unit_of_measurement"], "s")
        self.assertEqual(age["entity_category"], "diagnostic")

    def test_eeg_samples_entity(self):
        cfg = config_payloads(self.client)[
            "homeassistant/sensor/openmuse_0055da123456_eeg_samples/config"]
        self.assertEqual(cfg["state_class"], "total_increasing")
        self.assertEqual(cfg["unit_of_measurement"], "samples")
        self.assertEqual(cfg["value_template"],
                         "{{ value_json.eeg_samples | default('unknown') }}")

    def test_record_size_entity(self):
        cfg = config_payloads(self.client)[
            "homeassistant/sensor/openmuse_0055da123456_record_bytes/config"]
        self.assertEqual(cfg["device_class"], "data_size")
        self.assertEqual(cfg["unit_of_measurement"], "B")
        self.assertEqual(cfg["suggested_display_precision"], 0)

    def test_connected_binary_sensor(self):
        cfg = config_payloads(self.client)[
            "homeassistant/binary_sensor/openmuse_0055da123456_connected/config"]
        self.assertEqual(cfg["device_class"], "connectivity")
        self.assertEqual(cfg["value_template"],
                         "{{ 'ON' if value_json.connected else 'OFF' }}")

    def test_every_entity_references_availability_and_state_topic(self):
        for topic, cfg in config_payloads(self.client).items():
            self.assertEqual(cfg["availability_topic"],
                             "openmuse/openmuse_0055da123456/availability",
                             topic)
            self.assertEqual(cfg["payload_available"], "online")
            self.assertEqual(cfg["payload_not_available"], "offline")
            if topic != "homeassistant/binary_sensor/" \
                         "openmuse_0055da123456_connected/config":
                self.assertIn("value_json", cfg.get("value_template", ""))

    def test_discovery_retained(self):
        for topic, _, qos, retain in self.client.published:
            if topic.endswith("/config"):
                self.assertEqual((qos, retain), (1, True), topic)


class StatePublishTests(unittest.TestCase):
    def setUp(self):
        FakeClient.instances.clear()
        self.pub = make_publisher()
        self.pub.start()
        self.client = FakeClient.instances[-1]
        self.client.fire_connect()
        self.state_topic = "openmuse/openmuse_0055da123456/state"

    def state_messages(self) -> list[dict]:
        return [json.loads(p) for t, p, _, _ in self.client.published
                if t == self.state_topic]

    def test_state_json_contents(self):
        self.pub.publish(STATUS_ALIVE, SUPERVISOR_ALIVE)
        data = self.state_messages()[-1]
        self.assertEqual(data["state"], "streaming")
        self.assertIs(data["connected"], True)
        self.assertEqual(data["battery_percent"], 80)
        self.assertEqual(data["battery_time_to_empty_s"], 7200)
        self.assertEqual(data["last_data_age_s"], 0.4)
        self.assertEqual(data["eeg_samples"], 1234)
        self.assertEqual(data["record_bytes"], 5 * 1024 * 1024)
        # attribute fields
        self.assertEqual(data["samples"]["EEG"], 1234)
        self.assertEqual(data["record_file"], "/data/muse_2026-07-20_1200.txt")
        self.assertEqual(data["firmware"], "1.2.3")
        self.assertEqual(data["device_name"], "Muse S Athena")
        self.assertEqual(data["connect_count"], 2)
        self.assertEqual(data["failed_attempts"], 1)

    def test_null_numerics_omitted(self):
        status = dict(STATUS_ALIVE)
        status.update(battery_percent=None, battery_time_to_empty_s=None,
                      last_data_age_s=None, samples=None, record_bytes=None,
                      last_error=None)
        self.pub.publish(status, SUPERVISOR_ALIVE)
        data = self.state_messages()[-1]
        for key in ("battery_percent", "battery_time_to_empty_s",
                    "last_data_age_s", "eeg_samples", "record_bytes",
                    "last_error"):
            self.assertNotIn(key, data)
        self.assertIn("state", data)
        self.assertIn("connected", data)

    def test_child_dead_not_running(self):
        sup = dict(SUPERVISOR_ALIVE, child_alive=False)
        self.pub.publish(STATUS_ALIVE, sup)
        data = self.state_messages()[-1]
        self.assertEqual(data["state"], "not-running")
        self.assertIs(data["connected"], False)

    def test_rate_limiting_within_interval(self):
        clock = FakeClock(1000.0)
        self.pub._clock = clock
        self.pub.publish(STATUS_ALIVE, SUPERVISOR_ALIVE)
        clock.now = 1005.0  # 5 s later, inside the 10 s interval
        self.pub.publish(STATUS_ALIVE, SUPERVISOR_ALIVE)
        self.assertEqual(len(self.state_messages()), 1)
        clock.now = 1011.0  # 11 s after the first publish
        self.pub.publish(STATUS_ALIVE, SUPERVISOR_ALIVE)
        self.assertEqual(len(self.state_messages()), 2)

    def test_immediate_publish_on_state_change(self):
        clock = FakeClock(1000.0)
        self.pub._clock = clock
        self.pub.publish(STATUS_ALIVE, SUPERVISOR_ALIVE)
        clock.now = 1002.0
        status = dict(STATUS_ALIVE, state="reconnecting", connected=False)
        self.pub.publish(status, SUPERVISOR_ALIVE)
        data = self.state_messages()[-1]
        self.assertEqual(data["state"], "reconnecting")
        self.assertIs(data["connected"], False)
        self.assertEqual(len(self.state_messages()), 2)

    def test_state_retained(self):
        self.pub.publish(STATUS_ALIVE, SUPERVISOR_ALIVE)
        idx = [t for t, *_ in self.client.published].index(self.state_topic)
        _, _, qos, retain = self.client.published[idx]
        self.assertEqual((qos, retain), (1, True))


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        FakeClient.instances.clear()
        self.pub = make_publisher()
        self.pub.start()
        self.client = FakeClient.instances[-1]
        self.client.fire_connect()
        self.client.published.clear()

    def test_stop_publishes_offline(self):
        self.pub.publish(STATUS_ALIVE, SUPERVISOR_ALIVE)
        self.client.published.clear()
        self.pub.stop()
        offline = [(t, p, q, r) for t, p, q, r in self.client.published
                   if t.endswith("/availability")]
        self.assertEqual(offline,
                         [("openmuse/openmuse_0055da123456/availability",
                           "offline", 1, True)])
        self.assertTrue(self.client.disconnected)
        self.assertTrue(self.client.loop_stopped)

    def test_disconnect_no_offline_if_never_connected(self):
        # A client whose connect never succeeded: stop() must not claim
        # offline as a clean stop (the LWT covers the broker side).
        self.client.on_connect = None  # simulate: connect callback never ran
        self.pub._connected = False
        self.pub.stop()
        self.assertEqual(
            [t for t, *_ in self.client.published if "availability" in t], [])

    def test_resend_discovery_on_ha_status_online(self):
        self.pub.publish(STATUS_ALIVE, SUPERVISOR_ALIVE)
        self.client.published.clear()
        self.client.fire_message("homeassistant/status", b"online")
        configs = [t for t, *_ in self.client.published
                   if t.endswith("/config")]
        self.assertEqual(len(configs), 7)  # every entity resent
        # availability is refreshed too
        self.assertTrue(any(t.endswith("/availability")
                            for t, *_ in self.client.published))

    def test_ha_status_offline_ignored(self):
        self.client.fire_message("homeassistant/status", b"offline")
        self.assertEqual(
            [t for t, *_ in self.client.published if t.endswith("/config")], [])

    def test_subscribes_to_ha_status(self):
        self.assertIn(("homeassistant/status", 1), self.client.subscribed[0])


class ConnectFailureTests(unittest.TestCase):
    def setUp(self):
        FakeClient.instances.clear()

    def test_failed_connect_publishes_nothing(self):
        pub = make_publisher()
        pub.start()
        client = FakeClient.instances[-1]
        client.fire_connect(failed=True)
        pub.publish(STATUS_ALIVE, SUPERVISOR_ALIVE)
        self.assertEqual(client.published, [])

    def test_publish_while_disconnected_is_skipped_then_resumes(self):
        pub = make_publisher()
        pub.start()
        client = FakeClient.instances[-1]
        client.fire_connect()
        pub.publish(STATUS_ALIVE, SUPERVISOR_ALIVE)
        # simulate a drop: on_disconnect clears the flag
        client.on_disconnect(client, None, {}, 0, None)
        self.assertFalse(pub._connected)
        client.published.clear()
        pub.publish(STATUS_ALIVE, SUPERVISOR_ALIVE)
        self.assertEqual(
            [t for t, *_ in client.published
             if t.endswith("/state")], [])
        client.fire_connect()
        pub.publish(STATUS_ALIVE, SUPERVISOR_ALIVE)
        self.assertEqual(
            [t for t, *_ in client.published if t.endswith("/state")],
            ["openmuse/openmuse_0055da123456/state"])


if __name__ == "__main__":
    unittest.main()
