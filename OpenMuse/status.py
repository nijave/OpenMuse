"""Machine-readable status file for external dashboards.

`StatusWriter` maintains the status dict of a single streaming device and
writes it as JSON. Writes are atomic (a temporary file is written first and
then moved over the target with `os.replace`, so readers never observe a
half-written file) and can be rate-limited with `maybe_write()` for periodic
heartbeat updates while state transitions are written immediately via
`write()`.

The JSON schema (version 1) is documented in README.md; every field is always
present, with `null` when the value is unknown.
"""

import json
import math
import os
import time
from typing import Optional

SCHEMA_VERSION = 1

SAMPLE_KEYS = ("EEG", "ACCGYRO", "OPTICS", "BATTERY", "DRL")


def status_path_for_device(path: str, address: str) -> str:
    """Insert the sanitized device address before the extension of `path`.

    Mirrors the multi-device record filename logic in `OpenMuse.stream.stream`:
    "00:55:DA:B9:FA:20" becomes "0055DAB9FA20" and
    "status.json" -> "status_0055DAB9FA20.json".
    """
    sanitized = address.replace(":", "").replace("-", "")
    if "." in path:
        stem, ext = path.rsplit(".", 1)
        return f"{stem}_{sanitized}.{ext}"
    return f"{path}_{sanitized}"


class StatusWriter:
    """Hold and atomically write the JSON status of one streaming device."""

    def __init__(
        self,
        path: str,
        address: str,
        preset: str = "p1041",
        interval: float = 5.0,
    ):
        self.path = path
        self.interval = float(interval)
        self._last_write_monotonic = -math.inf
        self.data = {
            "version": SCHEMA_VERSION,
            "updated": None,
            "address": address,
            "preset": preset,
            "state": "connecting",
            "connected": False,
            "device_name": None,
            "firmware": None,
            "battery_percent": None,
            "battery_time_to_empty_s": None,
            "samples": {key: 0 for key in SAMPLE_KEYS},
            "last_data_age_s": None,
            "connected_since": None,
            "process_started": time.time(),
            "connect_count": 0,
            "failed_attempts": 0,
            "last_error": None,
            "record_file": None,
            "record_bytes": None,
        }

    def update(self, **fields) -> None:
        """Merge `fields` into the status dict (in memory, no write)."""
        samples = fields.pop("samples", None)
        if samples is not None:
            self.data["samples"].update(samples)
        was_connected = self.data.get("connected")
        self.data.update(fields)
        # Track when the device was (re)connected: set on every False -> True
        # transition, and kept while disconnected until the next connect.
        if self.data.get("connected") and not was_connected:
            self.data["connected_since"] = time.time()

    def write(self) -> None:
        """Atomically write the current status to `self.path`."""
        self.data["updated"] = time.time()
        self._refresh_record_bytes()
        snapshot = _jsonable(self.data)
        tmp_path = self.path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, self.path)
        self._last_write_monotonic = time.monotonic()

    def maybe_write(self) -> None:
        """Write only if at least `interval` seconds elapsed since the last write."""
        if time.monotonic() - self._last_write_monotonic >= self.interval:
            self.write()

    def _refresh_record_bytes(self) -> None:
        record_file = self.data.get("record_file")
        if record_file:
            try:
                self.data["record_bytes"] = os.path.getsize(record_file)
            except OSError:
                self.data["record_bytes"] = None


def _jsonable(value):
    """Return a JSON-serializable copy, mapping non-finite floats to None."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value
