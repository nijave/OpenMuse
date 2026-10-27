"""Tests for the optional auto-reconnect wrapper around _stream_async.

No real BLE is involved: _stream_async is monkeypatched with a fake coroutine
driven by a scripted sequence of outcomes, and time.monotonic is replaced with
a controllable fake clock so "ran for Ns" behaviour can be simulated.
"""

import asyncio
import importlib
import unittest
from unittest import mock

stream_mod = importlib.import_module("OpenMuse.stream")
from OpenMuse.cli import main
from OpenMuse.stream import StreamDisconnected, _stream_async_with_reconnect


class FakeClock:
    """Drop-in replacement for time.monotonic() with manual advancement."""

    def __init__(self, start=1000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class ScriptedStream:
    """Fake _stream_async driven by a scripted sequence of outcomes.

    Each script entry is one of:
      - "completed": clean end (returns normally)
      - "disconnected": link loss (raises StreamDisconnected immediately)
      - (ran_seconds, "disconnected"): link loss after advancing the fake
        clock by ran_seconds (simulates a successful streaming stretch)
      - "cancel": raises asyncio.CancelledError
    """

    def __init__(self, clock, script):
        self.clock = clock
        self.script = list(script)
        self.calls = []  # (duration, streams) per invocation

    async def __call__(
        self,
        address,
        preset,
        duration=None,
        raw_data_file=None,
        verbose=True,
        clock_model="windowed",
        sensors=None,
        streams=None,
    ):
        self.calls.append((duration, streams))
        step = self.script.pop(0)
        if isinstance(step, tuple):
            self.clock.advance(step[0])
            raise StreamDisconnected("link lost")
        if step == "completed":
            return
        if step == "cancel":
            raise asyncio.CancelledError()
        raise StreamDisconnected("link lost")

    @property
    def durations(self):
        return [d for d, _ in self.calls]

    @property
    def streams_passed(self):
        return [s for _, s in self.calls]


def run_wrapper(fake, **kwargs):
    kwargs.setdefault("reconnect_delay", 0)
    with mock.patch.object(stream_mod, "time", mock.Mock(monotonic=fake.clock)), \
            mock.patch.object(stream_mod, "_stream_async", fake):
        return asyncio.run(
            _stream_async_with_reconnect("AA:BB", "p1041", **kwargs)
        )


class ReconnectWrapperTests(unittest.TestCase):
    def _make(self, script):
        clock = FakeClock()
        fake = ScriptedStream(clock, script)
        return clock, fake

    def test_no_flag_single_call_no_retry(self):
        _, fake = self._make(["disconnected"])
        with self.assertRaises(StreamDisconnected):
            run_wrapper(fake, auto_reconnect=False)
        self.assertEqual(len(fake.calls), 1)

    def test_recovers_after_transient_disconnects(self):
        _, fake = self._make(
            ["disconnected", "disconnected", "completed"]
        )
        run_wrapper(fake, auto_reconnect=True, reconnect_attempts=5)
        self.assertEqual(len(fake.calls), 3)

    def test_outlets_reused_across_attempts(self):
        _, fake = self._make(["disconnected", "completed"])
        run_wrapper(fake, auto_reconnect=True, reconnect_attempts=5)
        self.assertIsNotNone(fake.streams_passed[0])
        self.assertIs(fake.streams_passed[0], fake.streams_passed[1])

    def test_gives_up_after_attempts_exhausted(self):
        _, fake = self._make(["disconnected"] * 5)
        with self.assertRaises(StreamDisconnected):
            run_wrapper(fake, auto_reconnect=True, reconnect_attempts=3)
        self.assertEqual(len(fake.calls), 3)

    def test_remaining_duration_passed_to_next_attempt(self):
        _, fake = self._make([(40.0, "disconnected"), "completed"])
        run_wrapper(
            fake, duration=100.0, auto_reconnect=True, reconnect_attempts=5
        )
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(fake.durations[0], 100.0)
        self.assertAlmostEqual(fake.durations[1], 60.0, places=6)

    def test_cancelled_error_propagates_immediately(self):
        _, fake = self._make(["cancel", "completed"])
        with self.assertRaises(asyncio.CancelledError):
            run_wrapper(fake, auto_reconnect=True, reconnect_attempts=5)
        self.assertEqual(len(fake.calls), 1)

    def test_attempt_counter_resets_after_long_successful_run(self):
        # attempts=2 budget, but each 40 s healthy stretch resets the counter,
        # so this sequence must run to completion.
        _, fake = self._make(
            [
                (40.0, "disconnected"),
                "disconnected",
                (40.0, "disconnected"),
                "disconnected",
                "completed",
            ]
        )
        run_wrapper(fake, auto_reconnect=True, reconnect_attempts=2)
        self.assertEqual(len(fake.calls), 5)


class CliReconnectFlagsTests(unittest.TestCase):
    def test_flags_reach_stream(self):
        with mock.patch.object(stream_mod, "stream") as mock_stream:
            rc = main(
                [
                    "stream",
                    "--address",
                    "AA:BB",
                    "--auto-reconnect",
                    "--reconnect-attempts",
                    "2",
                ]
            )
        self.assertEqual(rc, 0)
        mock_stream.assert_called_once()
        kwargs = mock_stream.call_args.kwargs
        self.assertTrue(kwargs["auto_reconnect"])
        self.assertEqual(kwargs["reconnect_attempts"], 2)
        self.assertEqual(kwargs["reconnect_delay"], 3.0)

    def test_defaults_when_flags_absent(self):
        with mock.patch.object(stream_mod, "stream") as mock_stream:
            main(["stream", "--address", "AA:BB"])
        kwargs = mock_stream.call_args.kwargs
        self.assertFalse(kwargs["auto_reconnect"])
        self.assertEqual(kwargs["reconnect_attempts"], 5)
        self.assertEqual(kwargs["reconnect_delay"], 3.0)


if __name__ == "__main__":
    unittest.main()