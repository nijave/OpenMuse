"""Unit tests for dashboard.py (no headband, no child process)."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
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


def make_supervisor(data_dir: Path, alive: bool = True,
                    env: dict | None = None,
                    popen=None) -> dashboard.Supervisor:
    config_env = {
        "MUSE_ADDRESS": "00:55:DA:00:00:00",
        "MUSE_DATA_DIR": str(data_dir),
        "MUSE_DASHBOARD_PORT": "0",
    }
    config_env.update(env or {})
    config = dashboard.Config(config_env)
    sup = dashboard.Supervisor(config)
    sup.started = time.time() - 90
    if popen is not None:
        sup._popen = popen
    # Fake child: poll() returns None while alive, an exit code when dead.
    sup.child = SimpleNamespace(pid=4242, poll=lambda: None if alive else 0,
                                returncode=0)
    return sup


class FakeProc:
    """Minimal subprocess.Popen double: poll() returns the exit code once set."""

    def __init__(self):
        self.returncode = None

    def poll(self):
        return self.returncode


class RecordingRunner:
    """Fake Popen: records the commands it is asked to run."""

    def __init__(self):
        self.commands: list[list[str]] = []
        self.procs: list[FakeProc] = []

    def __call__(self, cmd):
        self.commands.append(cmd)
        proc = FakeProc()
        self.procs.append(proc)
        return proc


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


class CompressConfigTests(unittest.TestCase):
    def test_defaults(self):
        config = dashboard.Config({
            "MUSE_ADDRESS": "00:55:DA:00:00:00",
            "MUSE_DATA_DIR": "/data",
        })
        self.assertEqual(config.compress, "zstd")
        self.assertEqual(config.compress_level, 3)

    def test_level_override(self):
        config = dashboard.Config({
            "MUSE_ADDRESS": "00:55:DA:00:00:00",
            "MUSE_DATA_DIR": "/data",
            "MUSE_COMPRESS_LEVEL": "9",
        })
        self.assertEqual(config.compress_level, 9)

    def test_disable_variants(self):
        for value in ("", "   ", "none", "NONE"):
            config = dashboard.Config({
                "MUSE_ADDRESS": "00:55:DA:00:00:00",
                "MUSE_DATA_DIR": "/data",
                "MUSE_COMPRESS": value,
            })
            self.assertIsNone(config.compress)

    def test_compress_command_shape(self):
        config = dashboard.Config({
            "MUSE_ADDRESS": "00:55:DA:00:00:00",
            "MUSE_DATA_DIR": "/data",
            "MUSE_COMPRESS_LEVEL": "7",
        })
        cmd = config.compress_command(Path("/data/muse_x.txt"))
        self.assertEqual(cmd, ["zstd", "-q", "-T1", "-7", "--rm",
                               "/data/muse_x.txt"])

    def test_compress_command_none_when_disabled(self):
        config = dashboard.Config({
            "MUSE_ADDRESS": "00:55:DA:00:00:00",
            "MUSE_DATA_DIR": "/data",
            "MUSE_COMPRESS": "none",
        })
        self.assertIsNone(config.compress_command(Path("/data/muse_x.txt")))


class CompressCandidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self.now = time.time()

    def age(self, path: Path, seconds: float) -> None:
        stamp = self.now - seconds
        os.utime(path, (stamp, stamp))

    def test_old_file_is_candidate(self):
        path = self.data_dir / "muse_2026-07-19_1200.txt"
        path.write_bytes(b"x")
        self.age(path, 3600)
        sup = make_supervisor(self.data_dir)
        self.assertEqual(sup.compress_candidates(now=self.now), [path])

    def test_live_file_skipped(self):
        live = self.data_dir / "muse_2026-07-20_1200.txt"
        live.write_bytes(b"x")
        self.age(live, 3600)
        other = self.data_dir / "muse_2026-07-19_1200.txt"
        other.write_bytes(b"y")
        self.age(other, 3600)
        sup = make_supervisor(self.data_dir)
        self.assertEqual(sup.compress_candidates(exclude=live, now=self.now),
                         [other])

    def test_recent_file_skipped(self):
        path = self.data_dir / "muse_2026-07-19_1200.txt"
        path.write_bytes(b"x")
        self.age(path, 30)  # modified inside the 60 s guard window
        sup = make_supervisor(self.data_dir)
        self.assertEqual(sup.compress_candidates(now=self.now), [])

    def test_file_with_zst_twin_skipped(self):
        path = self.data_dir / "muse_2026-07-19_1200.txt"
        path.write_bytes(b"x")
        self.age(path, 3600)
        (self.data_dir / "muse_2026-07-19_1200.txt.zst").write_bytes(b"z")
        sup = make_supervisor(self.data_dir)
        self.assertEqual(sup.compress_candidates(now=self.now), [])

    def test_only_muse_txt_files_considered(self):
        (self.data_dir / "status.json").write_text("{}")
        (self.data_dir / "muse_2026-07-19_1200.txt.zst").write_bytes(b"z")
        (self.data_dir / "other.txt").write_bytes(b"o")
        sup = make_supervisor(self.data_dir)
        self.assertEqual(sup.compress_candidates(now=self.now), [])

    def test_disabled_mode_no_candidates(self):
        path = self.data_dir / "muse_2026-07-19_1200.txt"
        path.write_bytes(b"x")
        self.age(path, 3600)
        sup = make_supervisor(self.data_dir, env={"MUSE_COMPRESS": "none"})
        self.assertEqual(sup.compress_candidates(now=self.now), [])


class CompressExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self.now = time.time()
        self.old_file = self.data_dir / "muse_2026-07-19_1200.txt"
        self.old_file.write_bytes(b"x")
        os.utime(self.old_file, (self.now - 3600, self.now - 3600))

    def test_scan_starts_compressor_for_old_file(self):
        runner = RecordingRunner()
        sup = make_supervisor(self.data_dir, popen=runner)
        started = sup.compress_scan(now=self.now)
        self.assertEqual(started, 1)
        self.assertEqual(runner.commands, [
            ["zstd", "-q", "-T1", "-3", "--rm", str(self.old_file)],
        ])
        self.assertEqual(sup.compressing(), ["muse_2026-07-19_1200.txt"])

    def test_scan_disabled_starts_nothing(self):
        runner = RecordingRunner()
        sup = make_supervisor(self.data_dir, env={"MUSE_COMPRESS": ""},
                              popen=runner)
        self.assertEqual(sup.compress_scan(now=self.now), 0)
        self.assertEqual(runner.commands, [])

    def test_scan_skips_in_flight_file(self):
        runner = RecordingRunner()
        sup = make_supervisor(self.data_dir, popen=runner)
        sup.compress_scan(now=self.now)
        self.assertEqual(sup.compress_scan(now=self.now), 0)
        self.assertEqual(len(runner.commands), 1)

    def test_logs_start_and_finish(self):
        runner = RecordingRunner()
        sup = make_supervisor(self.data_dir, popen=runner)
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            sup.compress_scan(now=self.now)
            runner.procs[0].returncode = 0
            sup.reap_compressors()
        out = captured.getvalue()
        self.assertIn("compressing: start", out)
        self.assertIn("compressing: finished muse_2026-07-19_1200.txt", out)

    def test_reap_logs_failure_and_clears(self):
        runner = RecordingRunner()
        sup = make_supervisor(self.data_dir, popen=runner)
        sup.compress_scan(now=self.now)
        runner.procs[0].returncode = 1
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            sup.reap_compressors()  # must not raise
        self.assertIn("compressing: failed muse_2026-07-19_1200.txt",
                      captured.getvalue())
        self.assertEqual(sup.compressing(), [])

    def test_popen_failure_is_logged_not_raised(self):
        def boom(cmd):
            raise OSError("no such binary")
        sup = make_supervisor(self.data_dir, popen=boom)
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            self.assertEqual(sup.compress_scan(now=self.now), 0)
        self.assertIn("compressing: failed to start", captured.getvalue())
        self.assertEqual(sup.compressing(), [])

    def test_supervisor_info_reports_compress(self):
        runner = RecordingRunner()
        sup = make_supervisor(self.data_dir, popen=runner)
        sup.compress_scan(now=self.now)
        info = sup.supervisor_info()
        self.assertEqual(info["compress"], "zstd")
        self.assertEqual(info["compressing"],
                         ["muse_2026-07-19_1200.txt"])
        runner.procs[0].returncode = 0
        sup.reap_compressors()
        info = sup.supervisor_info()
        self.assertEqual(info["compress"], "zstd")
        self.assertEqual(info["compressing"], [])

    def test_supervisor_info_compress_null_when_disabled(self):
        sup = make_supervisor(self.data_dir, env={"MUSE_COMPRESS": "none"})
        self.assertIsNone(sup.supervisor_info()["compress"])

    def test_index_page_shows_compressing_line(self):
        runner = RecordingRunner()
        sup = make_supervisor(self.data_dir, popen=runner)
        sup.compress_scan(now=self.now)
        html_page = dashboard.render_index(sup)
        self.assertIn("compressing: muse_2026-07-19_1200.txt", html_page)

    def test_index_page_no_compressing_line_when_idle(self):
        sup = make_supervisor(self.data_dir)
        self.assertNotIn("compressing:", dashboard.render_index(sup))

    @unittest.skipUnless(shutil.which("zstd"), "zstd binary not available")
    def test_real_zstd_round_trip(self):
        content = b"2025-09-25T08:11:28\tuuid\tdeadbeef\n" * 100
        old = self.data_dir / "muse_2026-07-19_0100.txt"
        old.write_bytes(content)
        os.utime(old, (self.now - 3600, self.now - 3600))
        sup = make_supervisor(self.data_dir, popen=subprocess.Popen)
        sup.compress_scan(now=self.now)
        proc = list(sup.compressors.values())[0]
        for _ in range(100):
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        self.assertEqual(proc.returncode, 0)
        zst = self.data_dir / "muse_2026-07-19_0100.txt.zst"
        self.assertTrue(zst.exists())
        self.assertFalse(old.exists())  # --rm removed the plain file
        out = subprocess.run(["zstd", "-dc", str(zst)],
                             capture_output=True, check=True).stdout
        self.assertEqual(out, content)


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
