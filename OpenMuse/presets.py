"""Muse preset catalogue.

Data sources:
- The vendor SDK's ``IXNMusePreset`` reference (documented preset list for
  Muse 2 / Muse S gen-2 / Muse S Athena headbands).
- A live 23-preset sweep on a Muse S Athena (MS-03, model name "muse2025")
  running firmware 3.1.29, 2026-10-27, confirming what each Athena preset
  actually streams.

All Athena presets that stream EEG do so at 14 bit / 256 Hz; IMU
(accelerometer + gyroscope) runs at 52 Hz, DRL/REF at 32 Hz, battery/status
at 1 Hz, and optics at 64 Hz. ``optics_power`` is the optics LED drive
("high" runs at roughly 1.5x the current of "low").

Unknown preset strings must still be sent to the device unchanged: the
firmware may accept presets that are not catalogued here. Use
:func:`warn_if_unknown_preset` to emit a warning instead of an error.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, fields
from typing import Dict, Iterable, List, Optional

__all__ = [
    "Preset",
    "PRESETS",
    "ATHENA_PRESETS",
    "DEFAULT_PRESET",
    "describe_preset",
    "preset_for_sensors",
    "format_preset_table",
    "warn_if_unknown_preset",
]

ATHENA_MODELS = ("muse2025",)


@dataclass(frozen=True)
class Preset:
    """One documented device preset."""

    name: str
    eeg_channels: int
    eeg_rate: float
    optics_channels: int
    optics_power: Optional[str]  # "low" / "high" / None (no optics)
    imu: bool
    drl_ref_rate: float  # 0 = no DRL/REF stream
    battery_rate: float
    ppg: bool
    thermistor: bool
    models: tuple[str, ...]
    note: str = ""


_A = ATHENA_MODELS  # shorthand for the table below

# Muse S Athena (muse2025) presets. Everything here was confirmed by the
# firmware 3.1.29 live sweep except p4129, whose sensor set was derived from
# the captured fixture tests/test_data/data_p4129.txt (EEG8 @ 256 Hz, optics4
# @ 64 Hz, IMU, DRL/REF, battery @ 1 Hz); its optics power is not visible in
# packet tags and is inferred as "low" from the README signature table.
_ATHENA_ROWS = [
    Preset("p1021", 4, 256, 0, None, True, 32, 1, False, False, _A, ""),
    Preset("p1022", 8, 256, 0, None, False, 32, 1, False, False, _A, ""),
    Preset("p1023", 0, 256, 0, None, False, 0, 50, False, False, _A,
           "battery/status only"),
    Preset("p1024", 0, 256, 0, None, True, 0, 1, False, False, _A, "IMU only"),
    Preset("p1025", 8, 256, 16, "low", True, 32, 1, False, False, _A, ""),
    Preset("p1026", 8, 256, 16, "high", True, 32, 1, False, False, _A, ""),
    Preset("p1027", 8, 256, 8, "low", True, 32, 1, False, False, _A, ""),
    Preset("p1028", 8, 256, 8, "high", True, 32, 1, False, False, _A, ""),
    Preset("p1029", 8, 256, 4, "low", True, 32, 1, False, False, _A, ""),
    Preset("p102a", 8, 256, 4, "high", True, 32, 1, False, False, _A, ""),
    Preset("p1031", 4, 256, 16, "low", True, 32, 1, False, False, _A, ""),
    Preset("p1032", 4, 256, 16, "high", True, 32, 1, False, False, _A, ""),
    Preset("p1033", 4, 256, 8, "low", True, 32, 1, False, False, _A, ""),
    Preset("p1034", 4, 256, 8, "high", True, 32, 1, False, False, _A,
           'Muse app "meditate"'),
    Preset("p1035", 4, 256, 4, "low", True, 32, 1, False, False, _A,
           'Muse app "sleep"'),
    Preset("p1036", 4, 256, 4, "high", True, 32, 1, False, False, _A, ""),
    Preset("p1041", 8, 256, 16, "low", True, 32, 1, False, False, _A,
           'OpenMuse default; Mind Monitor "16L"'),
    Preset("p1042", 8, 256, 16, "high", True, 32, 1, False, False, _A,
           'Mind Monitor "16H"'),
    Preset("p1043", 8, 256, 8, "low", True, 32, 1, False, False, _A,
           'Mind Monitor "8L"'),
    Preset("p1044", 8, 256, 8, "high", True, 32, 1, False, False, _A,
           'Mind Monitor "8H"'),
    Preset("p1045", 8, 256, 4, "low", True, 32, 1, False, False, _A,
           'Mind Monitor "4L"'),
    Preset("p1046", 8, 256, 4, "high", True, 32, 1, False, False, _A,
           'Mind Monitor "4H"'),
    Preset("p4129", 8, 256, 4, "low", True, 32, 1, False, False, _A,
           "device's resident preset after power-on; undocumented; sensor set "
           "derived from tests/test_data/data_p4129.txt; optics power "
           "inferred (low) from the README signature table"),
]

# Legacy presets accepted by the Athena firmware and by older Muse S / Muse 2.
# EEG is 12 bit / 256 Hz unless noted; "ppg" is 3-channel PPG at 64 Hz (not
# the Athena optics).
_LEGACY_ROWS = [
    Preset("p20", 5, 256, 0, None, True, 32, 0.1, False, False,
           ("muse2016", "muse2018", "muse2019", "muse2021", "muse2024"), ""),
    Preset("p21", 4, 256, 0, None, True, 32, 0.1, False, False,
           ("muse2016", "muse2018", "muse2019", "muse2021", "muse2024"),
           "default for muse2016/2018/2019/2021/2024; on Athena streams "
           "EEG4+IMU+DRL, no optics"),
    Preset("p22", 4, 256, 0, None, False, 32, 0.1, False, False,
           ("muse2016", "muse2018", "muse2019", "muse2021", "muse2024"), ""),
    Preset("p23", 5, 256, 0, None, False, 32, 0.1, False, False,
           ("muse2018", "muse2019", "muse2021", "muse2024"), ""),
    Preset("p50", 5, 256, 0, None, True, 32, 0.1, True, False,
           ("muse2018", "muse2019", "muse2021", "muse2024"), ""),
    Preset("p51", 4, 256, 0, None, True, 32, 0.1, True, False,
           ("muse2018", "muse2019", "muse2021", "muse2024"), ""),
    Preset("p52", 4, 256, 0, None, False, 32, 0.1, True, False,
           ("muse2018", "muse2019", "muse2021", "muse2024"), ""),
    Preset("p53", 6, 256, 0, None, True, 32, 0.1, True, False,
           ("muse2019", "muse2021", "muse2024"), ""),
    Preset("p54", 6, 128, 0, None, True, 16, 0.1, False, False,
           ("muse2021",), "EEG at 128 Hz"),
    Preset("p55", 4, 128, 0, None, False, 16, 0.1, True, False,
           ("muse2021",), "EEG at 128 Hz"),
    Preset("p60", 5, 256, 0, None, True, 32, 0.1, True, True,
           ("muse2019", "muse2021"), "+ thermistor 16 Hz"),
    Preset("p61", 4, 256, 0, None, True, 32, 0.1, True, True,
           ("muse2019", "muse2021"), "+ thermistor 16 Hz"),
    Preset("p63", 6, 256, 0, None, True, 32, 0.1, True, True,
           ("muse2019", "muse2021"), "+ thermistor 16 Hz"),
]

PRESETS: Dict[str, Preset] = {
    p.name: p for p in _ATHENA_ROWS + _LEGACY_ROWS
}

# The Muse S Athena preset set (the p10xx / p102a / p1041-p1046 sweep list).
# p4129 is also an Athena preset but is not part of the catalogued sweep set.
ATHENA_PRESETS: tuple[Preset, ...] = tuple(
    p for p in _ATHENA_ROWS if p.name != "p4129"
)

ATHENA_PRESET_NAMES = frozenset(p.name for p in ATHENA_PRESETS)

DEFAULT_PRESET = "p1041"


def describe_preset(name: str) -> Preset:
    """Return the :class:`Preset` for ``name`` (case-insensitive).

    Raises:
        KeyError: if ``name`` is not in the catalogue; the message lists the
            known preset names.
    """
    preset = PRESETS.get(name.lower())
    if preset is None:
        known = ", ".join(sorted(PRESETS))
        raise KeyError(
            f"Unknown preset {name!r}. Known presets: {known}. Unknown "
            "presets can still be sent to the device unchanged."
        )
    return preset


def preset_for_sensors(
    eeg: int = 8, optics: int = 16, power: str = "low"
) -> str:
    """Return the Athena preset name matching the requested sensor layout.

    Several Athena presets describe identical sensor layouts (the p104x
    "Mind Monitor" block duplicates the p102x/p103x research block). The
    default preset wins ties, then the lowest preset name; e.g. (8, 16,
    "low") resolves to p1041 and (8, 4, "high") to p102a.

    Raises:
        ValueError: if no catalogued Athena preset matches.
    """
    matches = [
        p.name
        for p in ATHENA_PRESETS
        if p.eeg_channels == eeg
        and p.optics_channels == optics
        and p.optics_power == power
    ]
    if not matches:
        raise ValueError(
            f"No Athena preset matches eeg={eeg}, optics={optics}, "
            f"power={power!r}."
        )
    if DEFAULT_PRESET in matches:
        return DEFAULT_PRESET
    return min(matches)


def format_preset_table(presets: Iterable[Preset]) -> str:
    """Format presets as an aligned plain-text table (no external deps)."""
    presets = list(presets)
    headers = (
        "preset",
        "eeg",
        "eeg_rate",
        "optics",
        "power",
        "imu",
        "drl_ref",
        "battery",
        "ppg",
        "thermistor",
        "models",
        "note",
    )
    rows = [
        (
            p.name,
            str(p.eeg_channels),
            _fmt_rate(p.eeg_rate),
            str(p.optics_channels),
            p.optics_power or "-",
            "yes" if p.imu else "no",
            _fmt_rate(p.drl_ref_rate),
            _fmt_rate(p.battery_rate),
            "yes" if p.ppg else "no",
            "yes" if p.thermistor else "no",
            "/".join(p.models),
            p.note,
        )
        for p in presets
    ]

    widths = [
        max(len(headers[i]), *(len(r[i]) for r in rows)) if rows
        else len(headers[i])
        for i in range(len(headers))
    ]

    def _fmt(cells: tuple[str, ...]) -> str:
        # Left-align the first and last two columns, right-align the rest.
        parts = []
        for i, cell in enumerate(cells):
            if i in (0, len(cells) - 2, len(cells) - 1):
                parts.append(cell.ljust(widths[i]))
            else:
                parts.append(cell.rjust(widths[i]))
        return "  ".join(parts)

    lines = [_fmt(headers), _fmt(tuple("-" * w for w in widths))]
    lines.extend(_fmt(r) for r in rows)
    return "\n".join(lines)


def _fmt_rate(rate: float) -> str:
    if rate == 0:
        return "-"
    if rate == int(rate):
        return f"{int(rate)} Hz"
    return f"{rate:g} Hz"


def warn_if_unknown_preset(name: str) -> None:
    """Warn (never error) if ``name`` is not in the catalogue.

    The caller must still send the preset string to the device unchanged:
    the firmware may accept presets we have not catalogued.
    """
    if name.lower() not in PRESETS:
        warnings.warn(
            f"Unknown preset {name!r}: it is not in the OpenMuse preset "
            "catalogue and will be sent to the device unchanged. Run "
            "`OpenMuse presets` to see documented presets.",
            UserWarning,
            stacklevel=2,
        )


def format_preset_fields(preset: Preset) -> str:
    """Format one preset as ``field: value`` lines (CLI --describe)."""
    return "\n".join(f"{f.name}: {getattr(preset, f.name)}" for f in fields(preset))
