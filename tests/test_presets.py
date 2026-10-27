"""Unit tests for the preset catalogue (OpenMuse/presets.py)."""

import contextlib
import io
import unittest
import warnings

from OpenMuse.cli import main
from OpenMuse.presets import (
    ATHENA_PRESETS,
    DEFAULT_PRESET,
    PRESETS,
    describe_preset,
    format_preset_table,
    preset_for_sensors,
    warn_if_unknown_preset,
)


class TestAthenaCatalogue(unittest.TestCase):
    """Every Athena preset from the fw 3.1.29 live sweep is present."""

    ATHENA_NAMES = [
        "p1021", "p1022", "p1023", "p1024",
        "p1025", "p1026", "p1027", "p1028", "p1029", "p102a",
        "p1031", "p1032", "p1033", "p1034", "p1035", "p1036",
        "p1041", "p1042", "p1043", "p1044", "p1045", "p1046",
        "p4129",
    ]

    def test_all_athena_presets_exist(self):
        for name in self.ATHENA_NAMES:
            with self.subTest(preset=name):
                self.assertIn(name, PRESETS)

    def test_athena_presets_are_muse2025(self):
        for preset in PRESETS.values():
            if preset.name in self.ATHENA_NAMES:
                with self.subTest(preset=preset.name):
                    self.assertIn("muse2025", preset.models)

    def test_athena_common_rates(self):
        # All catalogued Athena presets: EEG 256 Hz (when present),
        # DRL/REF 32 Hz (except the sensor-less presets), battery present.
        for preset in ATHENA_PRESETS:
            with self.subTest(preset=preset.name):
                self.assertEqual(preset.eeg_rate, 256)
                self.assertGreater(preset.battery_rate, 0)

    def test_expected_sensor_triples(self):
        expected = {
            "p1041": (8, 16, "low"),
            "p1042": (8, 16, "high"),
            "p1045": (8, 4, "low"),
            "p1035": (4, 4, "low"),
            "p1023": (0, 0, None),
            "p1024": (0, 0, None),
        }
        for name, triple in expected.items():
            preset = PRESETS[name]
            got = (preset.eeg_channels, preset.optics_channels, preset.optics_power)
            with self.subTest(preset=name):
                self.assertEqual(got, triple)

    def test_default_preset(self):
        self.assertEqual(DEFAULT_PRESET, "p1041")
        self.assertIn(DEFAULT_PRESET, PRESETS)


class TestPresetForSensors(unittest.TestCase):
    def test_known_mappings(self):
        self.assertEqual(preset_for_sensors(8, 16, "low"), "p1041")
        self.assertEqual(preset_for_sensors(4, 4, "high"), "p1036")
        self.assertEqual(preset_for_sensors(8, 4, "high"), "p102a")

    def test_defaults(self):
        self.assertEqual(preset_for_sensors(), "p1041")

    def test_no_match_raises(self):
        with self.assertRaises(ValueError):
            preset_for_sensors(5, 16, "low")


class TestDescribePreset(unittest.TestCase):
    def test_case_insensitive(self):
        self.assertEqual(describe_preset("P1041"), PRESETS["p1041"])
        self.assertEqual(describe_preset("P102A"), PRESETS["p102a"])

    def test_unknown_raises_keyerror_with_names(self):
        with self.assertRaises(KeyError) as ctx:
            describe_preset("p9999")
        self.assertIn("p1041", str(ctx.exception))


class TestWarnIfUnknownPreset(unittest.TestCase):
    def test_unknown_preset_warns(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            warn_if_unknown_preset("p9999")
        self.assertEqual(len(caught), 1)
        self.assertIs(caught[0].category, UserWarning)

    def test_known_preset_does_not_warn(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            warn_if_unknown_preset("p1041")
        self.assertEqual(len(caught), 0)

    def test_case_insensitive_lookup(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            warn_if_unknown_preset("P1041")
        self.assertEqual(len(caught), 0)


class TestFormatPresetTable(unittest.TestCase):
    def test_contains_every_name_one_row_per_preset(self):
        presets = list(PRESETS.values())
        table = format_preset_table(presets)
        lines = [line for line in table.splitlines() if line.strip()]
        # header + separator + one row per preset
        self.assertEqual(len(lines), len(presets) + 2)
        for preset in presets:
            with self.subTest(preset=preset.name):
                self.assertIn(preset.name, table)

    def test_columns_are_aligned(self):
        table = format_preset_table(ATHENA_PRESETS)
        widths = {len(line) for line in table.splitlines()}
        self.assertEqual(len(widths), 1)


class TestCliPresetsCommand(unittest.TestCase):
    def _run(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(argv)
        return code, out.getvalue()

    def test_default_shows_athena(self):
        code, out = self._run(["presets"])
        self.assertEqual(code, 0)
        self.assertIn("p1041", out)
        self.assertNotIn("p20 ", out)  # legacy presets not shown by default

    def test_legacy_model(self):
        code, out = self._run(["presets", "--model", "legacy"])
        self.assertEqual(code, 0)
        self.assertIn("p21", out)
        self.assertNotIn("p1041", out)

    def test_all_model(self):
        code, out = self._run(["presets", "--model", "all"])
        self.assertEqual(code, 0)
        self.assertIn("p1041", out)
        self.assertIn("p21", out)

    def test_describe_single_preset(self):
        code, out = self._run(["presets", "--describe", "p1041"])
        self.assertEqual(code, 0)
        self.assertIn("name: p1041", out)
        self.assertIn("eeg_channels: 8", out)
        self.assertIn("optics_power: low", out)


if __name__ == "__main__":
    unittest.main()
