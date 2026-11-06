"""Tests for lazy heavy imports (mne-lsl, vispy, matplotlib).

Verifies that the light surface of OpenMuse — ``import OpenMuse``,
``record``, ``find``/``presets`` CLI commands and ``decode_rawdata`` — works
with the heavy modules blocked, while the lazy attributes still resolve to
the right objects when the heavy dependencies are available.
"""

import subprocess
import sys
import textwrap
import unittest

_BLOCKED_SCRIPT = """
import contextlib
import io
import sys

# Block the heavy modules (and subpackages that would be imported) BEFORE
# importing OpenMuse.
for name in (
    "mne_lsl",
    "mne_lsl.lsl",
    "mne_lsl.stream",
    "mne_lsl.utils",
    "vispy",
    "vispy.app",
    "vispy.gloo",
    "vispy.util",
    "vispy.util.transforms",
    "vispy.visuals",
    "matplotlib",
    "matplotlib.pyplot",
):
    sys.modules[name] = None

import OpenMuse  # noqa: E402
from OpenMuse import cli  # noqa: E402  (submodule: import explicitly)

assert OpenMuse.__version__

# Eager, light surface.
assert callable(OpenMuse.record)
assert callable(OpenMuse.decode_rawdata)
assert callable(OpenMuse.parse_message)
assert callable(OpenMuse.find_muse)
assert callable(OpenMuse.MuseS)
assert callable(OpenMuse.describe_preset)
assert callable(OpenMuse.preset_for_sensors)
assert OpenMuse.PRESETS

# CLI commands that must not need the heavy modules.
for argv in (["presets"], ["--help"]):
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            rc = cli.main(argv)
    except SystemExit as e:
        rc = e.code
    assert rc in (0, None), (argv, rc)

# stream/view must fail with ImportError while the block is in place.
for name in ("stream", "view"):
    try:
        getattr(OpenMuse, name)
    except ImportError:
        pass
    else:
        raise AssertionError(f"OpenMuse.{name} should raise ImportError when blocked")

# drift_correction only needs numpy, so its lazy attributes still resolve.
assert callable(OpenMuse.correct_timestamps)
assert OpenMuse.DriftInfo is not None

print("BLOCKED-OK")
"""

_UNBLOCKED_SCRIPT = """
import importlib
import sys

import OpenMuse

# Importing the package must not pull in the heavy submodules (cli itself is
# light, so `from OpenMuse import cli` is allowed to work).
assert "OpenMuse.stream" not in sys.modules
assert "OpenMuse.view" not in sys.modules
assert "OpenMuse.drift_correction" not in sys.modules
assert "mne_lsl" not in sys.modules
assert "vispy" not in sys.modules
assert "matplotlib" not in sys.modules

# Lazy attributes resolve on first access to the very objects of their
# submodules.
stream_func = OpenMuse.stream
assert stream_func is importlib.import_module("OpenMuse.stream").stream
assert OpenMuse.stream is stream_func  # cached by __getattr__
drift_mod = importlib.import_module("OpenMuse.drift_correction")
assert OpenMuse.correct_timestamps is drift_mod.correct_timestamps
assert OpenMuse.DriftInfo is drift_mod.DriftInfo

# Accessing OpenMuse.stream pulled in mne-lsl.
assert "mne_lsl" in sys.modules

# PEP 562 keeps `from OpenMuse import <lazy>` working too.
from OpenMuse import stream as stream_via_from  # noqa: E402

assert stream_via_from is stream_func

print("UNBLOCKED-OK")
"""


class TestLazyImports(unittest.TestCase):
    @staticmethod
    def _run_snippet(code: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-c", textwrap.dedent(code).strip()],
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_import_and_cli_work_without_heavy_modules(self):
        proc = self._run_snippet(_BLOCKED_SCRIPT)
        self.assertEqual(
            proc.returncode,
            0,
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}",
        )
        self.assertIn("BLOCKED-OK", proc.stdout)

    def test_lazy_attributes_resolve_without_block(self):
        proc = self._run_snippet(_UNBLOCKED_SCRIPT)
        self.assertEqual(
            proc.returncode,
            0,
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}",
        )
        self.assertIn("UNBLOCKED-OK", proc.stdout)

    def test_all_covers_lazy_attributes(self):
        import OpenMuse

        for name in ("stream", "view", "correct_timestamps", "DriftInfo"):
            self.assertIn(name, OpenMuse.__all__)
        self.assertIn("__version__", OpenMuse.__all__)


if __name__ == "__main__":
    unittest.main()
