from __future__ import annotations

import json
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest

import numpy as np

from realtime_stability.acquisition import AcquisitionSession, SessionSettings
from realtime_stability.instrument import InstrumentSettings, Simulated53230A


class FaultSource(Simulated53230A):
    def __init__(self, settings, *, mode="silent"):
        super().__init__(settings)
        self.mode = mode
        self._overflow_once = True
        self.max_samples_per_segment = None

    def read_available(self):
        if self.mode == "silent":
            return np.empty(0)
        if self.mode == "invalid_final" and not self._running:
            return np.asarray([9.9e37])
        return super().read_available()

    def read_memory_overflow_event(self):
        if self.mode == "overflow" and self._overflow_once:
            self._overflow_once = False
            return True
        return False


class SessionIntegrationTests(unittest.TestCase):
    def test_simulated_session_streams_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            instrument = InstrumentSettings(host="simulation", gate_time_s=0.001)
            settings = SessionSettings(
                instrument=instrument,
                reference_hz=10e6,
                analysis_window_points=2000,
                output_root=Path(temporary),
                simulated=True,
                analysis_interval_s=0.02,
            )
            events = queue.Queue()
            session = AcquisitionSession(settings, Simulated53230A(instrument), events)
            session.start()
            deadline = time.monotonic() + 2.0
            while session.buffer.total_count < 30 and time.monotonic() < deadline:
                time.sleep(0.01)
            session.stop()
            self.assertTrue(session.wait(3.0))
            self.assertGreaterEqual(session.buffer.total_count, 30)
            self.assertIsNotNone(session.session_dir)
            session_dir = session.session_dir
            assert session_dir is not None
            rows = (session_dir / "measurements.csv").read_text(encoding="ascii").splitlines()
            self.assertEqual(len(rows), session.buffer.total_count + 1)
            metadata = json.loads((session_dir / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["status"], "completed")
            self.assertEqual(metadata["total_samples"], session.buffer.total_count)
            self.assertTrue((session_dir / "deviations_latest.csv").exists())

    def test_segment_boundaries_are_persisted_and_not_mixed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            instrument = InstrumentSettings(host="simulation", gate_time_s=0.001)
            source = Simulated53230A(instrument)
            source.max_samples_per_segment = 25
            settings = SessionSettings(
                instrument=instrument,
                reference_hz=10e6,
                analysis_window_points=200,
                output_root=Path(temporary),
                simulated=True,
                analysis_interval_s=0.01,
            )
            events = queue.Queue()
            session = AcquisitionSession(settings, source, events)
            session.start()
            deadline = time.monotonic() + 2.0
            while session.buffer.total_count < 65 and time.monotonic() < deadline:
                time.sleep(0.005)
            session.stop()
            self.assertTrue(session.wait(3.0))
            assert session.session_dir is not None
            data = np.genfromtxt(
                session.session_dir / "measurements.csv", delimiter=",", names=True
            )
            self.assertGreaterEqual(int(data["continuity_segment"].max()), 2)
            for segment in np.unique(data["continuity_segment"]):
                indices = data["segment_sample_index"][data["continuity_segment"] == segment]
                np.testing.assert_array_equal(indices, np.arange(indices.size))
            metadata = json.loads(
                (session.session_dir / "metadata.json").read_text(encoding="utf-8")
            )
            self.assertGreaterEqual(metadata["continuity_segment_count"], 3)
            self.assertFalse(metadata["gap_free_across_segment_boundaries"])

    def _run_fault_session(self, source, settings, stop_after=0.2):
        events = queue.Queue()
        session = AcquisitionSession(settings, source, events)
        session.start()
        time.sleep(stop_after)
        session.stop()
        self.assertTrue(session.wait(3.0))
        return session, list(iter_queue(events))

    def test_transient_overflow_is_recorded_as_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            instrument = InstrumentSettings(host="simulation", gate_time_s=0.001)
            settings = SessionSettings(
                instrument=instrument,
                reference_hz=10e6,
                analysis_window_points=200,
                output_root=Path(temporary),
                simulated=True,
                analysis_interval_s=0.01,
            )
            session, events = self._run_fault_session(FaultSource(instrument, mode="overflow"), settings)
            metadata = json.loads((session.session_dir / "metadata.json").read_text(encoding="utf-8"))
            self.assertTrue(metadata["overflow_detected"])
            self.assertEqual(metadata["status"], "error")
            self.assertTrue(any(event.kind == "error" for event in events))

    def test_no_first_reading_times_out(self):
        with tempfile.TemporaryDirectory() as temporary:
            instrument = InstrumentSettings(host="simulation", gate_time_s=0.001)
            settings = SessionSettings(
                instrument=instrument,
                reference_hz=10e6,
                analysis_window_points=200,
                output_root=Path(temporary),
                simulated=True,
                first_read_timeout_s=0.05,
                no_data_timeout_s=0.05,
            )
            session, events = self._run_fault_session(FaultSource(instrument, mode="silent"), settings, 0.15)
            self.assertFalse(session.is_running)
            self.assertTrue(any("readiness timeout" in event.message for event in events))

    def test_invalid_final_drain_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            instrument = InstrumentSettings(host="simulation", gate_time_s=0.001)
            settings = SessionSettings(
                instrument=instrument,
                reference_hz=10e6,
                analysis_window_points=200,
                output_root=Path(temporary),
                simulated=True,
                analysis_interval_s=0.01,
            )
            session, events = self._run_fault_session(
                FaultSource(instrument, mode="invalid_final"), settings, 0.05
            )
            metadata = json.loads((session.session_dir / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["status"], "error")
            self.assertEqual(metadata["rejected_readings"], 1)
            self.assertTrue(any("invalid/overload" in event.message for event in events))

    def test_session_can_be_reused_after_stop(self):
        with tempfile.TemporaryDirectory() as temporary:
            instrument = InstrumentSettings(host="simulation", gate_time_s=0.001)
            settings = SessionSettings(
                instrument=instrument,
                reference_hz=10e6,
                analysis_window_points=200,
                output_root=Path(temporary),
                simulated=True,
                analysis_interval_s=0.01,
            )
            session = AcquisitionSession(settings, Simulated53230A(instrument), queue.Queue())
            session.start()
            time.sleep(0.05)
            session.stop()
            self.assertTrue(session.wait(3.0))
            first_dir = session.session_dir
            session.start()
            time.sleep(0.05)
            session.stop()
            self.assertTrue(session.wait(3.0))
            self.assertGreater(session.buffer.total_count, 0)
            self.assertNotEqual(first_dir, session.session_dir)


def iter_queue(events):
    while True:
        try:
            yield events.get_nowait()
        except queue.Empty:
            return


if __name__ == "__main__":
    unittest.main()
