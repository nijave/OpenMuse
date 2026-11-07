"""Tests for OpenMuse.rawio: reading plain, gz and zst record files."""
from __future__ import annotations

import gzip
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import OpenMuse
from OpenMuse.rawio import read_rawdata_lines

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "test_data" / "data_p1035.txt"

LINES = [
    "2025-09-25T08:11:28.994204+00:00\t273e0013-4c4d-454d-96be-f03bac821358\tdeadbeef\n",
    "2025-09-25T08:11:29.113787+00:00\t273e0013-4c4d-454d-96be-f03bac821358\tcafebabe\n",
]


def _write_zst(src: Path, dst: Path) -> None:
    """Compress src into dst using zstandard if importable, else the binary."""
    try:
        import zstandard
    except ImportError:
        subprocess.run(["zstd", "-q", "-f", str(src), "-o", str(dst)],
                       check=True)
    else:
        data = src.read_bytes()
        with zstandard.open(dst, "wb") as fh:
            fh.write(data)


class RawIOTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_plain(self):
        path = self.dir / "rec.txt"
        path.write_text("".join(LINES), encoding="utf-8")
        self.assertEqual(read_rawdata_lines(path), [l.rstrip("\n") for l in LINES])
        # also accepts str paths
        self.assertEqual(read_rawdata_lines(str(path)),
                         [l.rstrip("\n") for l in LINES])

    def test_gz(self):
        path = self.dir / "rec.txt.gz"
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            fh.write("".join(LINES))
        self.assertEqual(read_rawdata_lines(path), [l.rstrip("\n") for l in LINES])

    def test_exported_from_package(self):
        self.assertIs(OpenMuse.read_rawdata_lines, read_rawdata_lines)
        self.assertIn("read_rawdata_lines", OpenMuse.__all__)

    @unittest.skipUnless(shutil.which("zstd") or _has_zstandard(),
                         "neither zstandard nor the zstd binary is available")
    def test_zst_round_trip(self):
        path = self.dir / "rec.txt.zst"
        _write_zst(_plain(self.dir), path)
        self.assertEqual(read_rawdata_lines(path), [l.rstrip("\n") for l in LINES])

    def test_missing_decoder_raises_importerror(self):
        import shutil as shutil_mod
        import sys
        from unittest import mock

        path = self.dir / "rec.txt.zst"
        path.write_bytes(b"\x28\xb5\x2f\xfd" + b"fake")
        # Hide both decoder options, whichever ones this machine has.
        with mock.patch.dict(sys.modules, {"zstandard": None}), \
                mock.patch.object(shutil_mod, "which", return_value=None):
            with self.assertRaises(ImportError) as ctx:
                read_rawdata_lines(path)
        self.assertIn("zstandard", str(ctx.exception))
        self.assertIn("zstd", str(ctx.exception))


def _has_zstandard() -> bool:
    try:
        import zstandard  # noqa: F401
    except ImportError:
        return False
    return True


def _plain(dir_: Path) -> Path:
    path = dir_ / "plain_src.txt"
    path.write_text("".join(LINES), encoding="utf-8")
    return path


class ZstDecodingParityTests(unittest.TestCase):
    """A small zst-compressed copy of a test fixture decodes identically."""

    @unittest.skipUnless(shutil.which("zstd") or _has_zstandard(),
                         "neither zstandard nor the zstd binary is available")
    def test_compressed_fixture_decodes_like_plain(self):
        import pandas as pd

        with open(FIXTURE, "r", encoding="utf-8") as f:
            small_lines = f.readlines()[:50]
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        dir_ = Path(self.tmp.name)
        plain = dir_ / "small.txt"
        plain.write_text("".join(small_lines), encoding="utf-8")
        zst = dir_ / "small.txt.zst"
        _write_zst(plain, zst)

        messages = read_rawdata_lines(zst)
        self.assertEqual(messages, [l.rstrip("\n") for l in small_lines])

        expected = OpenMuse.decode_rawdata(
            [l.rstrip("\n") for l in small_lines])
        got = OpenMuse.decode_rawdata(messages)
        sensor = "EEG" if "EEG" in got else next(iter(got))
        pd.testing.assert_frame_equal(got[sensor], expected[sensor])


if __name__ == "__main__":
    unittest.main()
