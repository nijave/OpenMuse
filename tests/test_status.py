"""Tests for the machine-readable status file writer (OpenMuse.status).

StatusWriter maintains the status dict for one device and writes it as JSON,
atomically (tmp file + os.replace), rate-limited by an interval.
"""

import importlib
import json
import math
import os
import tempfile
import unittest
from unittest import mock

status_mod = importlib.import_module("OpenMuse.status")
from OpenMuse.status import StatusWriter, status_path_for_device

EXPECTED_KEYS = {
    "version",
    "updated",
    "address",
    "preset",
    "state",
    "connected",
    "device_name",
    "firmware",
    "battery_percent",
    "battery_time_to_empty_s",
    "samples",
    "last_data_age_s",
    "connected_since",
    "process_started",
    "connect_count",
    "failed_attempts",
    "last_error",
    "record_file",
    "record_bytes",
}


class FakeClock:
    """Replacement for the time module inside OpenMuse.status."""

    def __init__(self, start=1000.0):
        self.t = start

    def monotonic(self):
        return self.t

    def time(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def make_writer(path, **kwargs):
    clock = FakeClock()
    writer = StatusWriter(path, address="00:55:DA:B9:FA:20", **kwargs)
    return clock, writer


class StatusWriterTests(unittest.TestCase):
    def test_writes_valid_json_with_every_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "status.json")
            clock, writer = make_writer(path)
            with mock.patch.object(status_mod, "time", clock):
                writer.update(
                    state="streaming",
                    connected=True,
                    device_name="Muse-A8A1",
                    firmware="10.92",
                    battery_percent=87.5,
                    battery_time_to_empty_s=3600.0,
                    last_data_age_s=0.2,
                    connect_count=1,
                    last_error=None,
                )
                writer.write()

                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)

            self.assertEqual(EXPECTED_KEYS, set(data.keys()))
            self.assertEqual(data["version"], 1)
            self.assertEqual(data["address"], "00:55:DA:B9:FA:20")
            self.assertEqual(data["preset"], "p1041")
            self.assertEqual(data["state"], "streaming")
            self.assertTrue(data["connected"])
            self.assertEqual(data["device_name"], "Muse-A8A1")
            self.assertEqual(data["firmware"], "10.92")
            self.assertEqual(data["battery_percent"], 87.5)
            self.assertEqual(data["battery_time_to_empty_s"], 3600.0)
            self.assertEqual(data["last_data_age_s"], 0.2)
            self.assertIsInstance(data["updated"], float)
            self.assertIsInstance(data["process_started"], float)
            self.assertIsInstance(data["connect_count"], int)
            self.assertIsInstance(data["failed_attempts"], int)
            self.assertIsNone(data["last_error"])
            self.assertIsNone(data["record_file"])
            self.assertIsNone(data["record_bytes"])
            self.assertEqual(
                set(data["samples"].keys()),
                {"EEG", "ACCGYRO", "OPTICS", "BATTERY", "DRL"},
            )
            self.assertTrue(
                all(isinstance(v, int) for v in data["samples"].values())
            )

    def test_unknown_fields_are_null(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "status.json")
            _, writer = make_writer(path)
            writer.write()
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertFalse(data["connected"])
            self.assertIsNone(data["device_name"])
            self.assertIsNone(data["firmware"])
            self.assertIsNone(data["battery_percent"])
            self.assertIsNone(data["battery_time_to_empty_s"])
            self.assertIsNone(data["last_data_age_s"])
            self.assertIsNone(data["connected_since"])
            self.assertIsNone(data["last_error"])

    def test_atomic_no_tmp_left_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "status.json")
            _, writer = make_writer(path)
            writer.update(state="stopped")
            writer.write()
            self.assertEqual(os.listdir(tmp), ["status.json"])

    def test_rate_limiting_honoured(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "status.json")
            clock, writer = make_writer(path, interval=5.0)
            with mock.patch.object(status_mod, "time", clock):
                writer.update(state="connecting")
                writer.maybe_write()
                with open(path, "r", encoding="utf-8") as f:
                    self.assertEqual(json.load(f)["state"], "connecting")

                # Within the interval: no rewrite, 'updated' stays the same.
                writer.update(state="streaming")
                writer.maybe_write()
                with open(path, "r", encoding="utf-8") as f:
                    self.assertEqual(json.load(f)["state"], "connecting")

                # Interval elapsed: rewrite happens.
                clock.advance(5.0)
                writer.maybe_write()
                with open(path, "r", encoding="utf-8") as f:
                    self.assertEqual(json.load(f)["state"], "streaming")

    def test_write_always_writes_regardless_of_interval(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "status.json")
            clock, writer = make_writer(path, interval=60.0)
            with mock.patch.object(status_mod, "time", clock):
                writer.update(state="connecting")
                writer.write()
                writer.update(state="streaming")
                writer.write()  # state transitions must not be rate-limited
                with open(path, "r", encoding="utf-8") as f:
                    self.assertEqual(json.load(f)["state"], "streaming")

    def test_nan_values_serialized_as_null(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "status.json")
            _, writer = make_writer(path)
            writer.update(battery_percent=float("nan"), last_data_age_s=math.inf)
            writer.write()
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
            self.assertNotIn("NaN", raw)
            self.assertNotIn("Infinity", raw)
            data = json.loads(raw)
            self.assertIsNone(data["battery_percent"])
            self.assertIsNone(data["last_data_age_s"])

    def test_record_bytes_reflects_record_file_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            record_path = os.path.join(tmp, "raw.txt")
            with open(record_path, "w", encoding="utf-8") as f:
                f.write("x" * 1234)

            path = os.path.join(tmp, "status.json")
            _, writer = make_writer(path)
            writer.update(record_file=record_path)
            writer.write()
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["record_file"], record_path)
            self.assertEqual(data["record_bytes"], 1234)

            # Growing the record file is picked up on the next write.
            with open(record_path, "a", encoding="utf-8") as f:
                f.write("y" * 10)
            writer.write()
            with open(path, "r", encoding="utf-8") as f:
                self.assertEqual(json.load(f)["record_bytes"], 1244)

    def test_record_bytes_null_without_record_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "status.json")
            _, writer = make_writer(path)
            writer.write()
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertIsNone(data["record_file"])
            self.assertIsNone(data["record_bytes"])

    def test_connected_since_set_on_first_connected_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "status.json")
            clock, writer = make_writer(path)
            with mock.patch.object(status_mod, "time", clock):
                self.assertIsNone(writer.data["connected_since"])
                writer.update(connected=True, state="streaming")
                self.assertEqual(writer.data["connected_since"], 1000.0)
                # A later disconnect keeps the last connect time; a reconnect
                # updates it.
                writer.update(connected=False)
                self.assertEqual(writer.data["connected_since"], 1000.0)
                clock.advance(30.0)
                writer.update(connected=True)
                self.assertEqual(writer.data["connected_since"], 1030.0)


class StatusPathForDeviceTests(unittest.TestCase):
    def test_inserts_sanitized_address_before_extension(self):
        self.assertEqual(
            status_path_for_device("status.json", "00:55:DA:B9:FA:20"),
            "status_0055DAB9FA20.json",
        )

    def test_path_without_extension(self):
        self.assertEqual(
            status_path_for_device("run/status", "00:55:DA:B9:FA:20"),
            "run/status_0055DAB9FA20",
        )

    def test_dashes_also_stripped(self):
        self.assertEqual(
            status_path_for_device("s.json", "AA-BB:CC"),
            "s_AABBCC.json",
        )


if __name__ == "__main__":
    unittest.main()
