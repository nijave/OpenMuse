"""OpenMuse: Minimal utilities for Muse EEG devices.

Light dependencies (bleak, numpy, pandas) are imported eagerly so that
``import OpenMuse``, the ``record`` / ``find`` / ``presets`` CLI commands and
``decode_rawdata`` work without mne-lsl, vispy or matplotlib installed.
The heavy features are exposed lazily via PEP 562 module-level
``__getattr__``:

- ``stream`` requires mne-lsl (OpenMuse.stream)
- ``view`` requires vispy and matplotlib (OpenMuse.view)
- ``correct_timestamps`` / ``DriftInfo`` (OpenMuse.drift_correction) are
  lazy only to keep the light import set minimal
"""

from .decode import decode_rawdata, parse_message
from .muse import find_muse
from .muse import MuseS
from .presets import PRESETS, describe_preset, preset_for_sensors
from .rawio import read_rawdata_lines
from .record import record

__version__ = "0.1.9"

# Attributes resolved on first access (PEP 562). Each entry maps the public
# name to the (module, attribute) it is fetched from.
_LAZY_ATTRS = {
    "stream": ("OpenMuse.stream", "stream"),
    "view": ("OpenMuse.view", "view"),
    "correct_timestamps": ("OpenMuse.drift_correction", "correct_timestamps"),
    "DriftInfo": ("OpenMuse.drift_correction", "DriftInfo"),
}

__all__ = [
    # eager (light dependencies only: bleak, numpy, pandas)
    "decode_rawdata",
    "parse_message",
    "find_muse",
    "MuseS",
    "PRESETS",
    "describe_preset",
    "preset_for_sensors",
    "read_rawdata_lines",
    "record",
    # lazy (heavy dependencies: mne-lsl, vispy, matplotlib)
    "stream",
    "view",
    "correct_timestamps",
    "DriftInfo",
    "__version__",
]


def __getattr__(name):
    try:
        module_name, attr_name = _LAZY_ATTRS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    from importlib import import_module

    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value  # cache for subsequent accesses
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY_ATTRS))
