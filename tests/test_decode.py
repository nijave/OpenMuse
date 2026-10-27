"""
Unit tests for the new decode module.

Tests the parse_message() and decode_rawdata() functions with real BLE message data from Muse devices.
The new decoder returns numpy arrays with timestamps instead of lists of dicts.
"""

import unittest
import os
import glob
from datetime import datetime

import numpy as np
from OpenMuse.decode import (
    parse_message,
    decode_rawdata,
    SENSORS,
    BATTERY_CHANNELS,
    make_timestamps,
    select_optics_channels,
    _decode_battery_data,
    _decode_drlref_data,
    _parse_packets,
    EEG_SCALE,
)


class TestGlobalTimestamping(unittest.TestCase):
    """Test suite for global timestamping logic using decode_rawdata() function."""

    @classmethod
    def setUpClass(cls):
        """Set up test fixtures - locate test data files."""
        cls.test_data_dir = os.path.join(os.path.dirname(__file__), "test_data")

        # All 15 presets for comprehensive testing
        cls.test_files = {
            "p20": os.path.join(cls.test_data_dir, "data_p20.txt"),
            "p21": os.path.join(cls.test_data_dir, "data_p21.txt"),
            "p50": os.path.join(cls.test_data_dir, "data_p50.txt"),
            "p51": os.path.join(cls.test_data_dir, "data_p51.txt"),
            "p60": os.path.join(cls.test_data_dir, "data_p60.txt"),
            "p61": os.path.join(cls.test_data_dir, "data_p61.txt"),
            "p1034": os.path.join(cls.test_data_dir, "data_p1034.txt"),
            "p1035": os.path.join(cls.test_data_dir, "data_p1035.txt"),
            "p1041": os.path.join(cls.test_data_dir, "data_p1041.txt"),
            "p1042": os.path.join(cls.test_data_dir, "data_p1042.txt"),
            "p1043": os.path.join(cls.test_data_dir, "data_p1043.txt"),
            "p1044": os.path.join(cls.test_data_dir, "data_p1044.txt"),
            "p1045": os.path.join(cls.test_data_dir, "data_p1045.txt"),
            "p1046": os.path.join(cls.test_data_dir, "data_p1046.txt"),
            "p4129": os.path.join(cls.test_data_dir, "data_p4129.txt"),
        }

        # Verify test data files exist
        for preset, filepath in cls.test_files.items():
            if not os.path.exists(filepath):
                raise FileNotFoundError(
                    f"Test data file not found: {filepath}\n"
                    f"Please ensure test data files are in {cls.test_data_dir}"
                )

    def test_basic_parsing(self):
        """Test basic parsing and global timestamping of multiple messages."""
        # Read multiple messages from p20 for global timestamping
        with open(self.test_files["p20"], "r", encoding="utf-8") as f:
            messages = [f.readline().strip() for _ in range(10) if f.readline().strip()]

        result = decode_rawdata(messages)

        # Should return a dict
        self.assertIsInstance(result, dict)

        # Should have all sensor type keys
        expected_keys = {"EEG", "ACCGYRO", "OPTICS", "BATTERY", "DRL", "Unknown"}
        self.assertEqual(set(result.keys()), expected_keys)

        # Each value should be a DataFrame
        import pandas as pd

        for key, value in result.items():
            self.assertIsInstance(value, pd.DataFrame)

    def test_accgyro_structure(self):
        """Test that ACCGYRO data has correct structure with global timestamping."""
        with open(self.test_files["p20"], "r", encoding="utf-8") as f:
            messages = [f.readline().strip() for _ in range(10) if f.readline().strip()]

        result = decode_rawdata(messages)
        accgyro_df = result["ACCGYRO"]

        # Should have data (p20 has ACCGYRO)
        self.assertGreater(len(accgyro_df), 0)

        # Should have 7 columns: time + 6 channels (ACC_X, ACC_Y, ACC_Z, GYRO_X, GYRO_Y, GYRO_Z)
        self.assertEqual(len(accgyro_df.columns), 7)

        # First column should be timestamps (increasing)
        timestamps = np.array(accgyro_df["time"])
        self.assertTrue(
            np.all(np.diff(timestamps) >= 0),
            "Timestamps should be monotonically increasing",
        )

    def test_eeg_structure(self):
        """Test that EEG data has correct structure with global timestamping."""
        with open(self.test_files["p1034"], "r", encoding="utf-8") as f:
            messages = [f.readline().strip() for _ in range(10) if f.readline().strip()]

        result = decode_rawdata(messages)
        eeg_df = result["EEG"]

        # Should have data (p1034 has EEG)
        self.assertGreater(len(eeg_df), 0)

        # Should have time + channels (either 4 or 8)
        self.assertIn(len(eeg_df.columns), [5, 9])  # time + 4 or time + 8

        # First column should be timestamps
        timestamps = np.array(eeg_df["time"])
        self.assertTrue(np.all(np.diff(timestamps) >= 0))

    def test_empty_message(self):
        """Test handling of empty message list."""
        result = decode_rawdata([])

        # Should return empty DataFrames for all sensor types
        import pandas as pd

        for key, value in result.items():
            self.assertIsInstance(value, pd.DataFrame)
            self.assertEqual(len(value), 0)

    def test_timestamp_uniformity(self):
        """Test that timestamps are uniformly spaced at correct sampling rate with global timestamping."""
        with open(self.test_files["p20"], "r", encoding="utf-8") as f:
            messages = [f.readline().strip() for _ in range(20) if f.readline().strip()]

        result = decode_rawdata(messages)
        accgyro_df = result["ACCGYRO"]

        if len(accgyro_df) > 2:
            timestamps = np.array(accgyro_df["time"])
            deltas = np.diff(timestamps)

            # Expected delta for 52 Hz ACCGYRO
            expected_delta = 1.0 / 52.0

            # With global timestamping, we expect most deltas to be close to expected,
            # but there may be larger gaps between messages
            small_gaps = deltas[
                deltas < expected_delta * 2
            ]  # Gaps smaller than 2x expected

            # At least 80% of gaps should be close to expected delta
            if len(small_gaps) > 0:
                self.assertTrue(
                    np.mean(np.abs(small_gaps - expected_delta) / expected_delta) < 0.1,
                    "Most timestamps should be uniformly spaced",
                )

    def test_sensors_config(self):
        """Test that SENSORS configuration is complete."""
        # Should have all expected sensor TAG bytes
        # 0x88 is a new firmware combined/metadata packet (structure TBD)
        expected_tags = {0x11, 0x12, 0x34, 0x35, 0x36, 0x47, 0x53, 0x88, 0x98}
        self.assertEqual(set(SENSORS.keys()), expected_tags)

        # Each sensor should have required fields
        for tag, config in SENSORS.items():
            self.assertIn("type", config)
            self.assertIn("n_channels", config)
            self.assertIn("n_samples", config)
            self.assertIn("rate", config)
            self.assertIn("data_len", config)

            # Validate types
            self.assertIsInstance(config["type"], str)
            self.assertIsInstance(config["n_channels"], int)
            self.assertIsInstance(config["n_samples"], int)
            self.assertIsInstance(config["rate"], float)
            self.assertIsInstance(config["data_len"], int)

    def test_performance(self):
        """Test that global timestamping decoding is fast enough for real-time use."""
        import time

        # Read 100 messages
        messages = []
        with open(self.test_files["p20"], "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= 100:
                    break
                messages.append(line.strip())

        # Time the decoding with global timestamping
        start = time.perf_counter()
        result = decode_rawdata(messages)
        elapsed = time.perf_counter() - start

        # Should be fast: < 10ms per message on average (global timestamping adds some overhead)
        ms_per_msg = (elapsed / len(messages)) * 1000
        self.assertLess(ms_per_msg, 10.0, f"Decoding too slow: {ms_per_msg:.3f} ms/msg")
        print(f"\nPerformance: {ms_per_msg:.3f} ms/msg")


class TestDecodeRawdata(unittest.TestCase):
    """Test suite for decode_rawdata() function."""

    @classmethod
    def setUpClass(cls):
        """Set up test fixtures."""
        cls.test_data_dir = os.path.join(os.path.dirname(__file__), "test_data")
        cls.test_file = os.path.join(cls.test_data_dir, "data_p20.txt")

    def test_basic_decoding(self):
        """Test basic multi-message decoding to DataFrames."""
        # Read 10 messages
        with open(self.test_file, "r", encoding="utf-8") as f:
            messages = [f.readline() for _ in range(10)]

        result = decode_rawdata(messages)

        # Should return a dict
        self.assertIsInstance(result, dict)

        # Should have all sensor type keys
        expected_keys = {"EEG", "ACCGYRO", "OPTICS", "BATTERY", "DRL", "Unknown"}
        self.assertEqual(set(result.keys()), expected_keys)

        # Each value should be a DataFrame
        import pandas as pd

        for key, value in result.items():
            self.assertIsInstance(value, pd.DataFrame)

    def test_accgyro_columns(self):
        """Test that ACCGYRO DataFrame has correct columns."""
        with open(self.test_file, "r", encoding="utf-8") as f:
            messages = [f.readline() for _ in range(10)]

        result = decode_rawdata(messages)
        accgyro_df = result["ACCGYRO"]

        # Should have data
        self.assertGreater(len(accgyro_df), 0)

        # Should have correct columns
        expected_columns = [
            "time",
            "ACC_X",
            "ACC_Y",
            "ACC_Z",
            "GYRO_X",
            "GYRO_Y",
            "GYRO_Z",
        ]
        self.assertEqual(list(accgyro_df.columns), expected_columns)

    def test_eeg_columns(self):
        """Test that EEG DataFrame has correct columns."""
        # Use p1034 which has EEG4 data
        test_file = os.path.join(self.test_data_dir, "data_p1034.txt")
        with open(test_file, "r", encoding="utf-8") as f:
            messages = [f.readline() for _ in range(10)]

        result = decode_rawdata(messages)
        eeg_df = result["EEG"]

        # Should have data
        if len(eeg_df) > 0:
            # Should have correct columns for EEG4
            expected_columns = ["time", "EEG_TP9", "EEG_AF7", "EEG_AF8", "EEG_TP10"]
            self.assertEqual(list(eeg_df.columns), expected_columns)

    def test_optics_columns(self):
        """Test that Optics DataFrame has correct columns."""
        # Use p1041 which has Optics16 data
        test_file = os.path.join(self.test_data_dir, "data_p1041.txt")
        with open(test_file, "r", encoding="utf-8") as f:
            messages = [f.readline() for _ in range(10)]

        result = decode_rawdata(messages)
        optics_df = result["OPTICS"]

        self.assertGreater(len(optics_df), 0)
        expected_columns = [
            "time",
            "OPTICS_LI_RED", "OPTICS_RI_RED", "OPTICS_LO_RED", "OPTICS_RO_RED",
            "OPTICS_LI_AMB", "OPTICS_RI_AMB", "OPTICS_LO_AMB", "OPTICS_RO_AMB",
            "OPTICS_LI_NIR", "OPTICS_RI_NIR", "OPTICS_LO_NIR", "OPTICS_RO_NIR",
            "OPTICS_LI_IR", "OPTICS_RI_IR", "OPTICS_LO_IR", "OPTICS_RO_IR",
        ]
        self.assertEqual(list(optics_df.columns), expected_columns)
        self.assertEqual(select_optics_channels(16), expected_columns[1:])

    def test_reduced_optics_channel_order(self):
        """The Optics16 correction must not relabel unvalidated Optics4/8 packets."""
        expected = {
            4: ["OPTICS_LI_NIR", "OPTICS_RI_NIR", "OPTICS_LI_IR", "OPTICS_RI_IR"],
            8: [
                "OPTICS_LO_NIR", "OPTICS_RO_NIR", "OPTICS_LO_IR", "OPTICS_RO_IR",
                "OPTICS_LI_NIR", "OPTICS_RI_NIR", "OPTICS_LI_IR", "OPTICS_RI_IR",
            ],
        }
        for count, channels in expected.items():
            with self.subTest(count=count):
                self.assertEqual(select_optics_channels(count), channels)

    def test_concatenation(self):
        """Test that multiple messages are properly concatenated with global timestamping."""
        # Read messages
        with open(self.test_file, "r", encoding="utf-8") as f:
            messages = [f.readline() for _ in range(5)]

        # Count total samples across all subpackets from all messages
        total_samples = 0
        for message in messages:
            parsed = parse_message(message)
            for subpacket in parsed["ACCGYRO"]:
                total_samples += subpacket["n_samples"]

        # Parse all messages together with global timestamping
        result = decode_rawdata(messages)
        combined_total = len(result["ACCGYRO"])

        # Should have same total samples
        self.assertEqual(total_samples, combined_total)


class TestBattery(unittest.TestCase):
    """Test suite for battery decoding."""

    @classmethod
    def setUpClass(cls):
        """Set up test fixtures."""
        cls.test_data_dir = os.path.join(os.path.dirname(__file__), "test_data")
        cls.battery_files = {
            "16_80": os.path.join(cls.test_data_dir, "test_battery_16_80.txt"),
            "58_27": os.path.join(cls.test_data_dir, "test_battery_58_27.txt"),
            "90_40": os.path.join(cls.test_data_dir, "test_battery_90_40.txt"),
        }

    def test_battery_known_levels(self):
        """Test battery decoding with files at known battery levels using global timestamping."""
        expected_values = {
            "16_80": 16.80,
            "58_27": 58.27,
            "90_40": 90.40,
        }

        for key, filepath in self.battery_files.items():
            # Read all messages and decode with global timestamping
            with open(filepath, "r", encoding="utf-8") as f:
                messages = [line.strip() for line in f if line.strip()]

            result = decode_rawdata(messages)
            battery_df = result["BATTERY"]

            # Should have found at least one battery sample
            self.assertGreater(len(battery_df), 0, f"No battery data found in {key}")

            # Check first battery value
            battery_percent = battery_df.iloc[
                0, 1
            ]  # First row, second column (after time)
            expected = expected_values[key]

            # Should be within 0.5% of expected
            self.assertAlmostEqual(
                float(battery_percent),
                expected,
                delta=0.5,
                msg=f"Battery level mismatch for {key}: got {float(battery_percent):.2f}, expected {expected:.2f}",
            )


    def test_battery_status_worked_example(self):
        """Decode the 10 fields of a real 0x88 status body (firmware 3.1.29)."""
        body = bytes.fromhex("d663c800c900d606ffffe00142722cce16ffe019")
        self.assertEqual(len(body), 20)

        decoded = _decode_battery_data(body, 0x88)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded.shape, (1, 10))

        expected = [
            99.8359375,  # BATTERY_PERCENT: 25558 / 256
            142.857142857,  # REMAINING_CAPACITY_MAH: 200 * 5/7
            143.571428571,  # FULL_CAPACITY_MAH: 201 * 5/7
            9843.75,  # TIME_TO_EMPTY_S: 1750 * 5.625
            np.nan,  # TIME_TO_FULL_S: 0xFFFF sentinel
            480.0,  # CYCLES: raw (1 LSB = 1 % of a cycle → ≈ 4.8 cycles)
            114.2578125,  # BATTERY_AGE_PERCENT: 29250 / 256
            4123.4375,  # VOLTAGE_MV: 52780 * 0.078125
            -36.5625,  # CURRENT_MA: int16 -234 * 0.15625
            25.875,  # TEMPERATURE_C: int16 6624 / 256
        ]
        np.testing.assert_allclose(decoded[0], expected, rtol=1e-6)
        self.assertTrue(np.isnan(decoded[0][4]))

    def test_battery_time_to_full_sentinel_is_conditional(self):
        """0xFFFF → NaN, but any other raw value scales normally."""
        body = bytearray(20)
        body[8:10] = (0x34, 0x12)  # idx 4 = 0x1234
        decoded = _decode_battery_data(bytes(body), 0x88)
        self.assertIsNotNone(decoded)
        self.assertFalse(np.isnan(decoded[0][4]))
        np.testing.assert_allclose(
            decoded[0][4], 0x1234 * 5.625, rtol=1e-6
        )

    def test_battery_short_body_returns_none(self):
        self.assertIsNone(_decode_battery_data(bytes(19), 0x88))
        self.assertIsNone(_decode_battery_data(bytes(19), 0x98))

    def test_legacy_battery_0x98_same_layout(self):
        """0x98 (data_p1041 era) uses the same 10-field layout as 0x88."""
        # Real 0x98 body from tests/test_data/data_p1041.txt: 80.22 %,
        # 100 remaining / 125 full mAh, not-charging sentinel, 134 cycle %,
        # 3885.5 mV, -55.6 mA discharge, 30.46 °C.
        body = bytes.fromhex("39508c00af006602ffff8600427247c29cfe761e")
        decoded = _decode_battery_data(body, 0x98)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded.shape, (1, 10))
        expected = [
            80.22265625,  # 20537 / 256
            100.0,  # 140 * 5/7
            125.0,  # 175 * 5/7
            3453.75,  # 614 * 5.625
            np.nan,  # 0xFFFF sentinel
            134.0,  # raw (1 LSB = 1 % of a cycle)
            114.2578125,  # 29250 / 256
            3885.546875,  # 49735 * 0.078125
            -55.625,  # int16 -356 * 0.15625
            30.4609375,  # int16 7798 / 256
        ]
        np.testing.assert_allclose(decoded[0], expected, rtol=1e-6)

    def test_battery_p1041_integration(self):
        """Integration: 0x88 status fields over the data_p1041 discharge capture."""
        filepath = os.path.join(self.test_data_dir, "data_p1041.txt")
        with open(filepath, "r", encoding="utf-8") as f:
            messages = [line.strip() for line in f if line.strip()]

        result = decode_rawdata(messages)
        battery_df = result["BATTERY"]

        self.assertGreater(len(battery_df), 0, "No battery data in data_p1041.txt")
        self.assertEqual(
            len(battery_df.columns),
            11,
            f"BATTERY should have 11 columns (time + 10), got {list(battery_df.columns)}",
        )
        self.assertEqual(
            list(battery_df.columns),
            ["time", *(c.lower() for c in BATTERY_CHANNELS)],
        )

        battery_percent = battery_df["battery_percent"]
        self.assertTrue(
            ((battery_percent >= 0) & (battery_percent <= 100)).all(),
            f"battery_percent out of [0, 100]: {battery_percent.min()}..{battery_percent.max()}",
        )

        voltage = battery_df["voltage_mv"]
        self.assertTrue(
            ((voltage >= 3000) & (voltage <= 4300)).all(),
            f"voltage_mv out of [3000, 4300]: {voltage.min()}..{voltage.max()}",
        )

        current = battery_df["current_ma"]
        self.assertTrue(
            (current < 0).all(),
            f"current_ma must be strictly negative (discharging), got {current.min()}..{current.max()}",
        )

        full_capacity = battery_df["full_capacity_mah"]
        self.assertEqual(
            full_capacity.nunique(),
            1,
            f"full_capacity_mah should be constant, got {full_capacity.unique()}",
        )

        temperature = battery_df["temperature_c"]
        self.assertTrue(
            ((temperature >= 10) & (temperature <= 45)).all(),
            f"temperature_c out of [10, 45]: {temperature.min()}..{temperature.max()}",
        )

        time_to_empty = battery_df["time_to_empty_s"]
        self.assertTrue(
            (time_to_empty > 0).all(),
            f"time_to_empty_s must be positive, got {time_to_empty.min()}..{time_to_empty.max()}",
        )


class TestNewFirmware(unittest.TestCase):
    """Test suite for new firmware data (2026+) with 0x88 battery packets and TAG variations."""

    @classmethod
    def setUpClass(cls):
        """Set up test fixtures."""
        cls.test_data_dir = os.path.join(os.path.dirname(__file__), "test_data")
        cls.new_firmware_file = os.path.join(cls.test_data_dir, "data_new_firmware.txt")
        cls.anomalous_file = os.path.join(
            cls.test_data_dir, "data_new_firmware_anomalous.txt"
        )

    def test_new_firmware_basic_parsing(self):
        """Test basic parsing of new firmware data with 0x88 battery packets."""
        with open(self.new_firmware_file, "r", encoding="utf-8") as f:
            messages = [line.strip() for line in f if line.strip()]

        result = decode_rawdata(messages)

        # Should have EEG data
        self.assertGreater(len(result["EEG"]), 0, "Should have EEG data")

        # Should have ACCGYRO data
        self.assertGreater(len(result["ACCGYRO"]), 0, "Should have ACCGYRO data")

        # Should have OPTICS data (16-channel format)
        self.assertGreater(len(result["OPTICS"]), 0, "Should have OPTICS data")

        # Should have BATTERY data (0x88 packets)
        self.assertGreater(
            len(result["BATTERY"]), 0, "Should have BATTERY data from 0x88 packets"
        )

    def test_new_firmware_eeg8_channels(self):
        """Test that new firmware EEG data has 8 channels (0x12 format)."""
        with open(self.new_firmware_file, "r", encoding="utf-8") as f:
            messages = [line.strip() for line in f if line.strip()]

        result = decode_rawdata(messages)
        eeg_df = result["EEG"]

        # Should have 9 columns: time + 8 EEG channels
        self.assertEqual(
            len(eeg_df.columns),
            9,
            f"EEG should have 9 columns (time + 8 channels), got {len(eeg_df.columns)}",
        )

    def test_new_firmware_optics16_channels(self):
        """Test that new firmware OPTICS data has 16 channels (0x36 format)."""
        with open(self.new_firmware_file, "r", encoding="utf-8") as f:
            messages = [line.strip() for line in f if line.strip()]

        result = decode_rawdata(messages)
        optics_df = result["OPTICS"]

        # Should have 17 columns: time + 16 OPTICS channels
        self.assertEqual(
            len(optics_df.columns),
            17,
            f"OPTICS should have 17 columns (time + 16 channels), got {len(optics_df.columns)}",
        )

    def test_new_firmware_battery_0x88(self):
        """Test that 0x88 battery packets are decoded correctly."""
        with open(self.new_firmware_file, "r", encoding="utf-8") as f:
            messages = [line.strip() for line in f if line.strip()]

        result = decode_rawdata(messages)
        battery_df = result["BATTERY"]

        # Should have battery data
        self.assertGreater(len(battery_df), 0)

        # Battery values should be in valid range (0-100%)
        battery_values = battery_df.iloc[:, 1].values  # Second column (after time)
        self.assertTrue(
            all(0 <= v <= 100 for v in battery_values),
            f"Battery values should be 0-100%, got: {battery_values}",
        )

    def test_anomalous_eeg4_packet(self):
        """Anomalous new-firmware capture yields EEG8 only, with no phantom EEG4.

        This capture carries no 0x11 (EEG4) at any valid packet boundary. The
        pre-0x88-framing-fix parser read the status packet as 188 bytes, swallowed
        the trailing subpackets, and fabricated a single phantom 0x11 EEG4 record.
        The fixed framing (PR #32) consumes 20 status bytes and preserves the real
        subpackets, so only genuine 0x12 (EEG8) records remain.
        """
        with open(self.anomalous_file, "r", encoding="utf-8") as f:
            messages = [line.strip() for line in f if line.strip()]

        found_eeg4 = False
        found_eeg8 = False
        for message in messages:
            parsed = parse_message(message)
            for subpacket in parsed.get("EEG", []):
                tag = subpacket.get("tag_byte")
                if tag == 0x11:
                    found_eeg4 = True
                    self.assertEqual(
                        subpacket["n_channels"], 4, "0x11 packet should have 4 channels"
                    )
                elif tag == 0x12:
                    found_eeg8 = True
                    # Verify 0x12 has 8 channels
                    self.assertEqual(
                        subpacket["n_channels"], 8, "0x12 packet should have 8 channels"
                    )

        self.assertFalse(
            found_eeg4, "Fixed framing must not fabricate a phantom 0x11 (EEG4) packet"
        )
        self.assertTrue(found_eeg8, "Should find at least one 0x12 (EEG8) packet")

    def test_anomalous_packet_data_validity(self):
        """Test that data from anomalous 0x11 packets has valid EEG values."""
        with open(self.anomalous_file, "r", encoding="utf-8") as f:
            messages = [line.strip() for line in f if line.strip()]

        for message in messages:
            parsed = parse_message(message)
            for subpacket in parsed.get("EEG", []):
                if subpacket.get("tag_byte") == 0x11:
                    data = subpacket["data"]
                    # Data should be in valid EEG range (0-1450 µV typically)
                    self.assertTrue(
                        np.all(data >= 0) and np.all(data <= 1500),
                        f"EEG4 data should be in valid range, got min={data.min()}, max={data.max()}",
                    )
                    # Shape should be (4, 4) for 4 samples × 4 channels
                    self.assertEqual(
                        data.shape,
                        (4, 4),
                        f"EEG4 data should be (4, 4), got {data.shape}",
                    )


class TestDRLRef(unittest.TestCase):
    """Test suite for DRL/REF (TAG 0x53) decoding."""

    @classmethod
    def setUpClass(cls):
        """Set up test fixtures."""
        cls.test_data_dir = os.path.join(os.path.dirname(__file__), "test_data")
        cls.p1041_file = os.path.join(cls.test_data_dir, "data_p1041.txt")

    def test_decode_drlref_data_worked_example(self):
        """Unit test for _decode_drlref_data with a real 24-byte 0x53 body.

        Captured on a live Muse S Athena (firmware 3.1.29). Layout: 6 samples
        x 2 channels, int16 LE, interleaved [DRL, REF, ...].
        """
        data_bytes = bytes.fromhex(
            "202045201f204c2020204a201f204b201f204c2020204c20"
        )
        self.assertEqual(len(data_bytes), 24)

        result = _decode_drlref_data(data_bytes)

        self.assertIsInstance(result, np.ndarray)
        self.assertEqual(result.shape, (6, 2))

        # Compare pre-scale integers so the test is independent of EEG_SCALE.
        expected_drl = np.array([8224, 8223, 8224, 8223, 8223, 8224])
        expected_ref = np.array([8261, 8268, 8266, 8267, 8268, 8268])
        raw = np.rint(result / EEG_SCALE).astype(int)

        np.testing.assert_array_equal(raw[:, 0], expected_drl)
        np.testing.assert_array_equal(raw[:, 1], expected_ref)

    def test_decode_drlref_data_short_input(self):
        """Truncated 0x53 bodies must be rejected, not decoded."""
        self.assertIsNone(_decode_drlref_data(b"\x00" * 23))

    def test_drlref_integration_p1041(self):
        """0x53 subpackets in data_p1041.txt must decode as a DRL stream.

        Regression intent: 0x53 was previously type "Unknown" and its records
        were dropped by _decode_subpacket_data.
        """
        with open(self.p1041_file, "r", encoding="utf-8") as f:
            messages = [line.strip() for line in f if line.strip()]

        result = decode_rawdata(messages)

        self.assertIn("DRL", result)
        drl_df = result["DRL"]
        self.assertGreater(len(drl_df), 0, "data_p1041.txt should contain 0x53 DRL data")

        # Columns: time + (DRL, REF)
        self.assertEqual(list(drl_df.columns), ["time", "DRL", "REF"])

        # Every decoded 0x53 subpacket reports 2 channels and 6 samples.
        seen_records = set()
        for message in messages:
            parsed = parse_message(message)
            for subpacket in parsed["DRL"]:
                seen_records.add((subpacket["n_channels"], subpacket["n_samples"]))

        self.assertEqual(seen_records, {(2, 6)})


class TestNoUnknownPacketTags(unittest.TestCase):
    """Guard: no capture fixture may carry a packet tag missing from SENSORS.

    _parse_packets silently drops packets whose pkt_id is not in SENSORS
    (pkt_type=None, pkt_valid=False). If a future firmware adds a sensor tag,
    that data would be lost with no error. This test walks every
    tests/test_data/data_*.txt fixture and fails loudly if any packet tag is
    unknown, so adding a tag means updating SENSORS.
    """

    @classmethod
    def setUpClass(cls):
        """Locate test fixtures."""
        cls.test_data_dir = os.path.join(os.path.dirname(__file__), "test_data")

    def test_no_fixture_carries_unknown_packet_tag(self):
        """Every packet tag in every data_*.txt fixture must be in SENSORS."""
        fixture_files = sorted(
            glob.glob(os.path.join(self.test_data_dir, "data_*.txt"))
        )
        self.assertGreater(
            len(fixture_files),
            0,
            "Expected at least one data_*.txt fixture in tests/test_data",
        )

        all_tags = set()

        for fixture in fixture_files:
            name = os.path.basename(fixture)
            with self.subTest(fixture=name):
                with open(fixture, "r", encoding="utf-8") as f:
                    lines = [line.strip() for line in f if line.strip()]

                unknown_tags = set()
                for line in lines:
                    # Mirror parse_message: skip malformed lines silently.
                    try:
                        ts, uuid, hexstring = line.split("\t", 2)
                        message_time = datetime.fromisoformat(
                            ts.replace("Z", "+00:00")
                        )
                        payload = bytes.fromhex(hexstring.strip())
                    except (ValueError, AttributeError):
                        continue

                    for pkt in _parse_packets(payload, message_time, uuid):
                        tag = pkt["pkt_id"]
                        all_tags.add(tag)
                        if pkt["pkt_type"] is None:
                            unknown_tags.add(tag)

                self.assertEqual(
                    unknown_tags,
                    set(),
                    f"{name}: unknown packet tag(s) "
                    + ", ".join(f"0x{tag:02x}" for tag in sorted(unknown_tags))
                    + " not in decode.SENSORS — add the tag or recapture the fixture",
                )

        # Sanity check the same property stated the other way: the walk must
        # have seen real tags, all of which belong to SENSORS.
        self.assertTrue(
            all_tags.issubset(set(SENSORS)),
            "Tags seen across fixtures missing from SENSORS: "
            + ", ".join(f"0x{tag:02x}" for tag in sorted(all_tags - set(SENSORS))),
        )
        self.assertGreater(
            len(all_tags),
            0,
            "No packet tags were seen across fixtures; the walk parsed nothing",
        )


class TestBatteryPercentGuard(unittest.TestCase):
    """A status body whose percent field is out of range decodes to NaN."""

    def test_out_of_range_percent_is_nan(self):
        from OpenMuse.decode import _decode_battery_data

        body = bytearray.fromhex("d663c800c900d606ffffe00142722cce16ffe019")
        body[0:2] = (150 * 256).to_bytes(2, "little")  # 150 % cannot be real
        out = _decode_battery_data(bytes(body), 0x88)
        self.assertTrue(np.isnan(out[0, 0]))
        # The remaining fields still decode (voltage from the same body).
        self.assertAlmostEqual(float(out[0, 7]), 4123.4375, places=3)

    def test_in_range_percent_unchanged(self):
        from OpenMuse.decode import _decode_battery_data

        body = bytes.fromhex("d663c800c900d606ffffe00142722cce16ffe019")
        out = _decode_battery_data(body, 0x88)
        self.assertAlmostEqual(float(out[0, 0]), 99.8359375, places=6)


if __name__ == "__main__":
    unittest.main()
