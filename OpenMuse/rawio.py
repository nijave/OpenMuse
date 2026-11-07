"""Read OpenMuse raw record files, transparently decompressing them.

``OpenMuse stream --record FILE`` writes one text line per BLE packet
(ISO timestamp, UUID, hex payload). Finished recordings may have been
compressed (``muse_*.txt.zst`` by the capture container, ``.gz`` anywhere);
:func:`read_rawdata_lines` returns the same list of lines regardless of
compression, so it can replace ``open()``/``readlines()``.

Only the standard library is required for plain and ``.gz`` files. ``.zst``
files need either the ``zstandard`` package (``pip install OpenMuse[zstd]``)
or the ``zstd`` command-line binary on PATH.
"""
from __future__ import annotations

import gzip
import shutil
import subprocess
from pathlib import Path

__all__ = ["read_rawdata_lines"]


def read_rawdata_lines(path: str | Path) -> list[str]:
    """Return the record lines of a plain, gzip- or zstd-compressed file.

    Parameters
    ----------
    path : str | Path
        Record file: ``*.txt`` (plain text), ``*.gz`` (gzip) or ``*.zst``
        (zstd). Selection is by file suffix; the file must be decompressed
        with the format its name claims.

    Returns
    -------
    list[str]
        One string per line, without trailing newlines (drop-in input for
        :func:`OpenMuse.decode_rawdata`).

    Raises
    ------
    ImportError
        For ``.zst`` files when neither the ``zstandard`` package nor the
        ``zstd`` binary is available.
    """
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            data = fh.read()
    elif suffix == ".zst":
        data = _read_zstd(path).decode("utf-8")
    else:
        data = path.read_text(encoding="utf-8")
    return data.splitlines()


def _read_zstd(path: Path) -> bytes:
    """Decompress a .zst file with `zstandard` if importable, else the CLI."""
    try:
        import zstandard
    except ImportError:
        binary = shutil.which("zstd")
        if binary is None:
            raise ImportError(
                f"cannot read {path}: decompressing .zst records requires "
                "either the 'zstandard' package (pip install zstandard, or "
                "pip install 'OpenMuse[zstd]') or the 'zstd' command-line "
                "binary on PATH"
            ) from None
        try:
            proc = subprocess.run(
                [binary, "-dc", str(path)],
                capture_output=True,
                check=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RuntimeError(f"zstd failed to decompress {path}: {exc}") from exc
        return proc.stdout
    with zstandard.open(path, "rb") as fh:
        return fh.read()
