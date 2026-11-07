#!/usr/bin/env python3
"""Supervisor + status dashboard for overnight OpenMuse capture.

Runs `OpenMuse stream ...` as a child process, rotates the record file once a
day at MUSE_ROTATE_AT local time, and serves a tiny HTML status page (plus
/status.json and /healthz) so a headless box can be checked from a phone.
The child's stdout/stderr are passed through, so journald picks them up.

Standard library only; Python 3.12.
"""
from __future__ import annotations

import html
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Badge colour per status state (grey covers not-running/unknown, amber the
# transient connect states, green streaming, red anything requiring attention).
STATE_COLORS = {
    "not-running": "grey",
    "unknown": "grey",
    "connecting": "amber",
    "reconnecting": "amber",
    "streaming": "green",
    "stalled": "red",
    "failed": "red",
    "stopped": "red",
}

# Preferred row order for the status table; anything else is appended.
STATUS_ORDER = [
    "version", "updated", "address", "preset", "state", "connected",
    "device_name", "firmware", "battery_percent", "battery_time_to_empty_s",
    "samples", "last_data_age_s", "connected_since", "process_started",
    "connect_count", "failed_attempts", "last_error", "record_file",
    "record_bytes",
]

STALE_AFTER_S = 30
HEALTHZ_MAX_AGE_S = 60


def log(msg: str) -> None:
    print(f"[dashboard] {datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def parse_rotate_at(value: str | None) -> tuple[int, int] | None:
    """Parse a local 'HH:MM' into (hour, minute). '' or None disables."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    parts = value.split(":")
    if len(parts) != 2 or not all(p.isdigit() and len(p) <= 2 for p in parts):
        raise ValueError(f"MUSE_ROTATE_AT must be 'HH:MM', got {value!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if hour > 23 or minute > 59:
        raise ValueError(f"MUSE_ROTATE_AT must be 'HH:MM', got {value!r}")
    return hour, minute


def next_rotation_ts(hm: tuple[int, int] | None) -> float | None:
    """Unix timestamp of the next occurrence of the rotate time."""
    if hm is None:
        return None
    hour, minute = hm
    now = datetime.now()
    cand = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if cand <= now:
        cand += timedelta(days=1)
    return cand.timestamp()


class Config:
    """All knobs come from the environment (quadlet EnvironmentFile)."""

    def __init__(self, env: dict[str, str] | None = None):
        env = os.environ if env is None else env
        self.address = env.get("MUSE_ADDRESS", "")
        if not self.address:
            raise SystemExit("MUSE_ADDRESS is required (e.g. 00:55:DA:00:00:00)")
        self.preset = env.get("MUSE_PRESET", "p1035")
        self.data_dir = Path(env.get("MUSE_DATA_DIR", "/data"))
        self.port = int(env.get("MUSE_DASHBOARD_PORT", "8088"))
        self.bind = env.get("MUSE_DASHBOARD_BIND", "0.0.0.0")
        self.rotate_display = env.get("MUSE_ROTATE_AT", "12:00").strip() or None
        self.rotate_at = parse_rotate_at(self.rotate_display)
        self.reconnect_delay = int(env.get("MUSE_RECONNECT_DELAY", "10"))
        self.reconnect_attempts = int(env.get("MUSE_RECONNECT_ATTEMPTS", "1000000"))
        self.extra_args = shlex.split(env.get("MUSE_EXTRA_ARGS", ""))
        # Compress finished record files with this tool (""/"none" disables).
        compress = env.get("MUSE_COMPRESS", "zstd").strip()
        self.compress = compress.lower() if compress.lower() not in ("", "none") \
            else None
        self.compress_level = int(env.get("MUSE_COMPRESS_LEVEL", "3"))

    def command(self, record_file: Path) -> list[str]:
        return [
            "OpenMuse", "stream",
            "--address", self.address,
            "--preset", self.preset,
            "--auto-reconnect",
            "--reconnect-attempts", str(self.reconnect_attempts),
            "--reconnect-delay", str(self.reconnect_delay),
            "--record", str(record_file),
            "--status-file", str(self.data_dir / "status.json"),
            "--status-interval", "5",
            *self.extra_args,
        ]

    def compress_command(self, record_file: Path) -> list[str] | None:
        """zstd command that compresses a finished file in place, or None."""
        if self.compress is None:
            return None
        return [self.compress, "-q", "-T1", f"-{self.compress_level}",
                "--rm", str(record_file)]


# --------------------------------------------------------------------------
# Supervisor
# --------------------------------------------------------------------------

class Supervisor:
    """Owns the stream child process; shared with the HTTP thread."""

    # Files modified more recently than this may still be written by a
    # just-stopped child; leave them for a later scan.
    COMPRESS_MIN_AGE_S = 60
    # How often the supervisor rescans for compressible leftovers.
    COMPRESS_SCAN_INTERVAL_S = 60

    def __init__(self, config: Config):
        self.config = config
        self.child: subprocess.Popen | None = None  # or a test double
        self.record_file: Path | None = None  # file the child writes to
        self.stop_requested = False
        self.started = time.time()
        self.rotations = 0
        # Running compressor subprocesses, keyed by record file name.
        self.compressors: dict[str, subprocess.Popen] = {}
        # Injectable for tests; real value spawns the compressor process.
        self._popen = subprocess.Popen

    # -- child ----------------------------------------------------------

    @property
    def child_alive(self) -> bool:
        try:
            return self.child is not None and self.child.poll() is None
        except Exception:
            return False

    def next_record_file(self) -> Path:
        return self.config.data_dir / f"muse_{datetime.now():%Y-%m-%d_%H%M}.txt"

    def start_child(self, record: Path | None = None) -> None:
        record = self.next_record_file() if record is None else record
        self.record_file = record
        cmd = self.config.command(record)
        log(f"starting: {shlex.join(cmd)}")
        self.child = subprocess.Popen(cmd)

    # -- compression of finished record files ---------------------------

    def compress_candidates(self, exclude: Path | None = None,
                            now: float | None = None) -> list[Path]:
        """Finished muse_*.txt files that are safe to compress now.

        Skips the file the live child writes (``exclude``), files modified
        in the last COMPRESS_MIN_AGE_S seconds, files that already have a
        ``.zst`` twin, and files already being compressed.
        """
        if self.config.compress is None:
            return []
        now = time.time() if now is None else now
        exclude = exclude.resolve() if exclude is not None else None
        out: list[Path] = []
        try:
            entries = sorted(self.config.data_dir.glob("muse_*.txt"))
        except OSError:
            return []
        for path in entries:
            if exclude is not None:
                try:
                    if path.resolve() == exclude:
                        continue
                except OSError:
                    continue
            if path.name in self.compressors:
                continue
            twin = path.with_name(path.name + ".zst")
            if twin.exists():
                continue
            try:
                if now - path.stat().st_mtime < self.COMPRESS_MIN_AGE_S:
                    continue
            except OSError:
                continue
            out.append(path)
        return out

    def start_compression(self, path: Path) -> bool:
        """Start one background zstd run; never raises, only logs."""
        cmd = self.config.compress_command(path)
        if cmd is None:
            return False
        log(f"compressing: start {shlex.join(cmd)}")
        try:
            proc = self._popen(cmd)
        except OSError as exc:
            log(f"compressing: failed to start {path.name}: {exc}")
            return False
        self.compressors[path.name] = proc
        return True

    def compress_scan(self, exclude: Path | None = None,
                      now: float | None = None) -> int:
        """Compress every currently compressible leftover; returns count."""
        candidates = self.compress_candidates(exclude=exclude, now=now)
        return sum(1 for path in candidates if self.start_compression(path))

    def reap_compressors(self) -> None:
        """Log and forget finished compressor subprocesses."""
        for name, proc in list(self.compressors.items()):
            code = proc.poll()
            if code is None:
                continue
            del self.compressors[name]
            if code == 0:
                log(f"compressing: finished {name}")
            else:
                log(f"compressing: failed {name} (exit code {code})")

    def compressing(self) -> list[str]:
        return sorted(self.compressors)

    def rotate(self) -> None:
        log("rotation time reached; restarting the stream with a new record file")
        self.rotations += 1
        self.stop_child()
        record = self.next_record_file()
        self.compress_scan(exclude=record)
        self.start_child(record)

    def run(self) -> int:
        """Main loop; returns the supervisor (and process) exit code."""
        record = self.next_record_file()
        # Compress leftovers from a previous run, but never the file the
        # new child is about to write.
        self.compress_scan(exclude=record)
        self.start_child(record)
        next_rotate = next_rotation_ts(self.config.rotate_at)
        last_scan = time.time()
        while not self.stop_requested:
            if next_rotate is not None and time.time() >= next_rotate:
                self.rotate()
                next_rotate = next_rotation_ts(self.config.rotate_at)
                last_scan = time.time()
            elif not self.child_alive:
                code = self.child.returncode if self.child is not None else -1
                log(f"stream child exited with code {code}; supervisor exits "
                    f"(systemd Restart= handles retries)")
                return code
            self.reap_compressors()
            if time.time() - last_scan >= self.COMPRESS_SCAN_INTERVAL_S:
                self.compress_scan(exclude=self.record_file)
                last_scan = time.time()
            time.sleep(1.0)
        # SIGTERM/SIGINT was forwarded by the handler; give the child time.
        if self.child is not None and self.child.poll() is None:
            try:
                self.child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self._signal_child(self.child, signal.SIGKILL)
        return 0

    def stop_child(self, grace_s: int = 30) -> None:
        """SIGINT, wait, escalate to SIGTERM and SIGKILL if needed."""
        child = self.child
        if child is None or child.poll() is not None:
            return
        self._signal_child(child, signal.SIGINT)
        try:
            child.wait(timeout=grace_s)
            return
        except subprocess.TimeoutExpired:
            pass
        log("child did not exit after SIGINT; sending SIGTERM")
        self._signal_child(child, signal.SIGTERM)
        try:
            child.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            pass
        log("child did not exit after SIGTERM; sending SIGKILL")
        self._signal_child(child, signal.SIGKILL)
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass

    @staticmethod
    def _signal_child(child: subprocess.Popen, signum: int) -> None:
        try:
            child.send_signal(signum)
        except ProcessLookupError:
            pass

    def rotate(self) -> None:
        log("rotation time reached; restarting the stream with a new record file")
        self.rotations += 1
        self.stop_child()
        self.start_child()

    def run(self) -> int:
        """Main loop; returns the supervisor (and process) exit code."""
        self.start_child()
        next_rotate = next_rotation_ts(self.config.rotate_at)
        while not self.stop_requested:
            if next_rotate is not None and time.time() >= next_rotate:
                self.rotate()
                next_rotate = next_rotation_ts(self.config.rotate_at)
            elif not self.child_alive:
                code = self.child.returncode if self.child is not None else -1
                log(f"stream child exited with code {code}; supervisor exits "
                    f"(systemd Restart= handles retries)")
                return code
            time.sleep(1.0)
        # SIGTERM/SIGINT was forwarded by the handler; give the child time.
        if self.child is not None and self.child.poll() is None:
            try:
                self.child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self._signal_child(self.child, signal.SIGKILL)
        return 0

    # -- status ----------------------------------------------------------

    def status(self) -> tuple[dict | None, float | None]:
        """Return (status.json contents, age in seconds). Both None if the
        file is missing or malformed -- the HTTP thread must not crash."""
        path = self.config.data_dir / "status.json"
        try:
            st = path.stat()
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None, None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None, None
        if not isinstance(data, dict):
            return None, None
        return data, max(0.0, time.time() - st.st_mtime)

    def supervisor_info(self) -> dict:
        _, age = self.status()
        return {
            "child_alive": self.child_alive,
            "child_pid": self.child.pid if self.child is not None else None,
            "uptime_s": round(time.time() - self.started, 1),
            "status_age_s": round(age, 1) if age is not None else None,
            "rotate_at": self.config.rotate_display,
            "data_dir": str(self.config.data_dir),
            "compress": self.config.compress,
            "compressing": self.compressing(),
        }


# --------------------------------------------------------------------------
# Rendering helpers
# --------------------------------------------------------------------------

def fmt_bytes(value) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return str(value)
    n = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_seconds(value) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return str(value)
    total = int(value)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m or h:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return f"{' '.join(parts)} ({value:g} s)"


def fmt_timestamp(value) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value).isoformat(sep=" ", timespec="seconds")
    return str(value)


def fmt_value(key: str, value) -> str:
    if key in ("updated", "connected_since", "process_started"):
        return fmt_timestamp(value)
    if key == "record_bytes" or key.endswith("_bytes"):
        return fmt_bytes(value)
    if key.endswith("_s") and isinstance(value, (int, float)) \
            and not isinstance(value, bool):
        return fmt_seconds(value)
    if key == "battery_percent":
        return f"{value}%"
    if isinstance(value, dict):
        return ", ".join(f"{k}: {v}" for k, v in value.items())
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def badge(sup: Supervisor) -> tuple[str, str]:
    """(state, colour) for the big badge."""
    status, _ = sup.status()
    if not sup.child_alive:
        state = "not-running"
    elif status is None:
        state = "unknown"
    else:
        state = str(status.get("state", "unknown"))
    return state, STATE_COLORS.get(state, "grey")


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="5">
<title>OpenMuse capture</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 1rem; background: #111;
         color: #eee; }}
  h1 {{ font-size: 1.2rem; }}
  h2 {{ font-size: 1rem; margin-top: 1.5rem; }}
  .badge {{ display: inline-block; padding: 0.5rem 1.5rem; border-radius: 8px;
           font-size: 1.8rem; font-weight: 700; letter-spacing: 0.03em; }}
  .grey  {{ background: #555; }}
  .amber {{ background: #b8860b; }}
  .green {{ background: #1e7a34; }}
  .red   {{ background: #b02a2a; }}
  table {{ border-collapse: collapse; width: 100%; max-width: 40rem;
           font-size: 0.9rem; }}
  td, th {{ border: 1px solid #333; padding: 0.25rem 0.5rem; text-align: left;
            word-break: break-word; }}
  th {{ color: #9ab; font-weight: 600; }}
  .muted {{ color: #9ab; }}
  .stale {{ color: #ff9d3c; font-weight: 700; }}
</style>
</head>
<body>
<h1>OpenMuse capture</h1>
<p><span class="badge {color}">{state}</span></p>
<p class="muted">supervisor up {uptime} &middot; child pid {child_pid}
({child_state}) &middot; status.json {status_age}{stale}</p>
{compressing_html}
<table>
<tr><th>field</th><th>value</th></tr>
{rows}
</table>
<h2>Files in {data_dir}</h2>
<table>
<tr><th>file</th><th>size</th><th>modified</th></tr>
{files}
</table>
</body>
</html>
"""


def list_files(data_dir: Path, cap: int = 30) -> list[tuple[str, str, str]]:
    """Newest first (name, human size, local mtime), capped."""
    try:
        entries = [p for p in data_dir.iterdir() if p.is_file()]
    except OSError:
        return []
    entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for p in entries[:cap]:
        try:
            st = p.stat()
        except OSError:
            continue
        out.append((
            p.name,
            fmt_bytes(st.st_size),
            datetime.fromtimestamp(st.st_mtime).isoformat(sep=" ",
                                                          timespec="seconds"),
        ))
    return out


def render_index(sup: Supervisor) -> str:
    state, color = badge(sup)
    status, age = sup.status()
    status_age = "no status yet" if age is None else f"{age:.0f} s old"
    stale = ' <span class="stale">STALE</span>' if age is not None \
        and age > STALE_AFTER_S else ""

    rows: list[str] = []
    if status is None:
        rows.append('<tr><td colspan="2">no status yet</td></tr>')
    else:
        keys = [k for k in STATUS_ORDER if k in status]
        keys += sorted(k for k in status if k not in STATUS_ORDER)
        for k in keys:
            rows.append(f"<tr><th>{html.escape(k)}</th>"
                        f"<td>{html.escape(fmt_value(k, status[k]))}</td></tr>")
    rows_html = "\n".join(rows)

    files = list_files(sup.config.data_dir)
    files_html = "\n".join(
        f"<tr><td>{html.escape(name)}</td><td>{html.escape(size)}</td>"
        f"<td>{html.escape(mtime)}</td></tr>"
        for name, size, mtime in files
    ) or '<tr><td colspan="3">none</td></tr>'

    uptime = fmt_seconds(round(time.time() - sup.started))
    child_pid = sup.child.pid if sup.child is not None else None
    running = sup.compressing()
    compressing_html = \
        f'<p class="muted">compressing: {html.escape(", ".join(running))}</p>' \
        if running else ""

    return PAGE.format(
        state=html.escape(state),
        color=color,
        uptime=uptime,
        child_pid=child_pid if child_pid is not None else "-",
        child_state="alive" if sup.child_alive else "dead",
        status_age=status_age,
        stale=stale,
        compressing_html=compressing_html,
        rows=rows_html,
        data_dir=html.escape(str(sup.config.data_dir)),
        files=files_html,
    )


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "openmuse-dashboard/1.0"
    supervisor: Supervisor  # bound in make_server()

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        path = self.path.split("?", 1)[0]
        if len(path) > 1:
            path = path.rstrip("/")
        try:
            if path == "/" or path == "":
                self._send(200, "text/html; charset=utf-8",
                           render_index(self.supervisor).encode("utf-8"))
            elif path == "/status.json":
                status, _ = self.supervisor.status()
                payload = dict(status) if status else {}
                payload["supervisor"] = self.supervisor.supervisor_info()
                self._send(200, "application/json",
                           json.dumps(payload).encode("utf-8"))
            elif path == "/healthz":
                _, age = self.supervisor.status()
                ok = self.supervisor.child_alive and age is not None \
                    and age < HEALTHZ_MAX_AGE_S
                body = b"ok\n" if ok else b"unhealthy\n"
                self._send(200 if ok else 503, "text/plain; charset=utf-8", body)
            else:
                self._send(404, "text/plain; charset=utf-8", b"not found\n")
        except BrokenPipeError:
            pass

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        log(f"{self.address_string()} {fmt % args}")


def make_server(sup: Supervisor, bind: str | None = None,
                port: int | None = None) -> ThreadingHTTPServer:
    bound = type("BoundHandler", (Handler,), {"supervisor": sup})
    return ThreadingHTTPServer(
        (sup.config.bind if bind is None else bind,
         sup.config.port if port is None else port),
        bound,
    )


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main() -> int:
    config = Config()
    try:
        config.data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log(f"cannot create data dir {config.data_dir}: {exc}")
    sup = Supervisor(config)

    server = make_server(sup)
    threading.Thread(target=server.serve_forever, name="dashboard-http",
                     daemon=True).start()
    log(f"dashboard on http://{config.bind}:{config.port}/")

    def forward(signum, _frame) -> None:
        # Always SIGINT the child, even on SIGTERM (what `podman stop` and
        # systemd send): OpenMuse handles KeyboardInterrupt by closing the
        # record file and writing a final "stopped" status, while SIGTERM
        # would kill Python outright and lose the buffered tail of the file.
        log(f"received {signal.Signals(signum).name}; sending SIGINT to child")
        sup.stop_requested = True
        if sup.child is not None and sup.child.poll() is None:
            sup._signal_child(sup.child, signal.SIGINT)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)

    try:
        code = sup.run()
    finally:
        server.shutdown()
    return code


if __name__ == "__main__":
    sys.exit(main())
