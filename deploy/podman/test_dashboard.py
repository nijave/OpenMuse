"""Unit tests for dashboard.py (no headband, no child process)."""
from __future__ import annotations

import importlib.util
import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("dashboard", HERE / "dashboard.py")
dashboard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dashboard)

FRESH_STATUS = {
    "version": "0.1.9",
    "updated": time.time(),
    "address": "00:55:DA:00:00:00",
    "preset": "p1035",
    "state": "streaming",
    "connected": True,
    "device_name": "Muse S Athena",
    "firmware": "1.0",
    "battery_percent": 80,
    "battery_time_to_empty_s": 7200,
    "samples": {"TP9": 100, "AF7": 101},
    "last_data_age_s": 0.4,
    "connected_since": time.time() - 60,
    "process_started": time.time() - 120,
    "connect_count": 1,
    "failed_attempts": 0,
    "last_error": None,
    "record_file": "/data/muse_2026-07-20_1200.txt",
    "record_bytes": 5 * 1024 * 1024,
}


def make_supervisor(data_dir: Path, alive: bool = True) -> dashboard.Supervisor:
    config = dashboard.Config({
        "MUSE_ADDRESS": "00:55:DA:00:00:00",
        "MUSE_DATA_DIR": str(data_dir),
        "MUSE_DASHBOARD_PORT": "0",
    })
    sup = dashboard.Supervisor(config)
    sup.started = time.time() - 90
    # Fake child: poll() returns None while alive, an exit code when dead.
    sup.child = SimpleNamespace(pid=4242, poll=lambda: None if alive else 0,
                                returncode=0)
    return sup


def serve(sup: dashboard.Supervisor):
    server = dashboard.make_server(sup, bind="127.0.0.1", port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}"


class DashboardHTTPTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        (self.data_dir / "status.json").write_text(json.dumps(FRESH_STATUS))
        (self.data_dir / "muse_2026-07-20_1200.txt").write_bytes(b"x" * 2048)
        (self.data_dir / "muse_2026-07-19_1200.txt").write_bytes(b"y" * 512)
        self.sup = make_supervisor(self.data_dir, alive=True)
        self.server, self.base = serve(self.sup)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.tmp.cleanup)

    def get(self, path: str):
        with urllib.request.urlopen(self.base + path, timeout=5) as resp:
            return resp.status, resp.read().decode()

    def test_index_ok_contains_state(self):
        code, body = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn("streaming", body)
        self.assertIn("4242", body)  # child pid shown
        self.assertIn("Muse S Athena", body)
        self.assertIn("muse_2026-07-20_1200.txt", body)  # file listing
        self.assertIn("2.0 KB", body)  # human file sizes

    def test_status_json_merges_supervisor(self):
        code, body = self.get("/status.json")
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertEqual(data["state"], "streaming")
        sup = data["supervisor"]
        self.assertEqual(sup["child_alive"], True)
        self.assertEqual(sup["child_pid"], 4242)
        self.assertGreater(sup["uptime_s"], 0)
        self.assertLess(sup["status_age_s"], 30)
        self.assertEqual(sup["rotate_at"], "12:00")
        self.assertEqual(sup["data_dir"], str(self.data_dir))

    def test_healthz_ok_when_alive_and_fresh(self):
        code, _ = self.get("/healthz")
        self.assertEqual(code, 200)

    def test_healthz_503_when_child_dead(self):
        sup = make_supervisor(self.data_dir, alive=False)
        # Point the fake dead child at the shared status dir.
        old_child, self.sup.child = self.sup.child, sup.child
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.get("/healthz")
            self.assertEqual(ctx.exception.code, 503)
        finally:
            self.sup.child = old_child

    def test_missing_status_renders_no_status_yet(self):
        (self.data_dir / "status.json").unlink()
        code, body = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn("no status yet", body)
        # /healthz is 503 without a status file even though the child is alive
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/healthz")
        self.assertEqual(ctx.exception.code, 503)
        # /status.json still returns supervisor info
        code, body = self.get("/status.json")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["supervisor"]["child_alive"], True)

    def test_malformed_status_renders_no_status_yet(self):
        (self.data_dir / "status.json").write_text("{not json")
        code, body = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn("no status yet", body)

    def test_unknown_path_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_status_goes_stale(self):
        import os
        path = self.data_dir / "status.json"
        path.write_text(json.dumps(dict(FRESH_STATUS)))
        old_time = time.time() - 3600
        os.utime(path, (old_time, old_time))
        _, body = self.get("/")
        self.assertIn("STALE", body)


class RotationParsingTests(unittest.TestCase):
    def test_parse_valid(self):
        self.assertEqual(dashboard.parse_rotate_at("12:00"), (12, 0))
        self.assertEqual(dashboard.parse_rotate_at("00:30"), (0, 30))
        self.assertEqual(dashboard.parse_rotate_at("23:59"), (23, 59))

    def test_parse_empty_disables(self):
        self.assertIsNone(dashboard.parse_rotate_at(""))
        self.assertIsNone(dashboard.parse_rotate_at("   "))
        self.assertIsNone(dashboard.parse_rotate_at(None))

    def test_parse_invalid(self):
        for bad in ("24:00", "12:60", "12", "ab:cd", "-1:00", "12:00:00"):
            with self.assertRaises(ValueError):
                dashboard.parse_rotate_at(bad)

    def test_next_rotation_ts(self):
        self.assertIsNone(dashboard.next_rotation_ts(None))
        ts = dashboard.next_rotation_ts((23, 59))
        self.assertIsNotNone(ts)
        self.assertGreater(ts, time.time())

    def test_command_shape(self):
        config = dashboard.Config({
            "MUSE_ADDRESS": "00:55:DA:00:00:00",
            "MUSE_DATA_DIR": "/data",
            "MUSE_EXTRA_ARGS": "--preset-extra foo",
        })
        cmd = config.command(Path("/data/muse_x.txt"))
        self.assertEqual(cmd[0], "OpenMuse")
        self.assertEqual(cmd[1], "stream")
        self.assertIn("--auto-reconnect", cmd)
        self.assertIn("--status-file", cmd)
        self.assertEqual(cmd[cmd.index("--status-file") + 1],
                         "/data/status.json")
        self.assertEqual(cmd[cmd.index("--reconnect-delay") + 1], "10")
        self.assertEqual(cmd[-2:], ["--preset-extra", "foo"])


if __name__ == "__main__":
    unittest.main()
