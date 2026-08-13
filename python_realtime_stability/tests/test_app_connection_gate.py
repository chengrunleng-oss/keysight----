from __future__ import annotations

import queue
import tkinter as tk
import unittest
from unittest.mock import patch

from realtime_stability.acquisition import SessionEvent
from realtime_stability.app import StabilityAnalyzerApp


class AppConnectionGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.app = StabilityAnalyzerApp()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"Tk display unavailable: {exc}") from exc
        cls.app.withdraw()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.app.destroy()

    def setUp(self) -> None:
        self.app.events = queue.Queue()
        self.app.session = None
        self.app.connection_gate.invalidate()
        self.app._update_start_button_state()

    def _complete_success(self) -> None:
        fingerprint = self.app._connection_fingerprint()
        test_id = self.app.connection_gate.begin_test(fingerprint)
        self.app.events.put(
            SessionEvent(
                "connection_test_ok",
                message="KEYSIGHT TECHNOLOGIES,53230A,SERIAL,FW",
                connection_fingerprint=fingerprint,
                connection_test_id=test_id,
            )
        )
        with patch("realtime_stability.app.messagebox.showinfo"):
            self.app._poll_events()

    def _complete_parameter_success(self) -> None:
        fingerprint = self.app._parameter_fingerprint()
        test_id = self.app.connection_gate.begin_parameter_test(fingerprint)
        self.app.events.put(
            SessionEvent(
                "parameter_test_ok",
                parameter_fingerprint=fingerprint,
                connection_test_id=test_id,
            )
        )
        with patch("realtime_stability.app.messagebox.showinfo"):
            self.app._poll_events()

    def test_initial_button_is_disabled_and_success_enables_current_settings(self) -> None:
        self.assertEqual(str(self.app.start_button["state"]), "disabled")
        self._complete_success()
        self.assertEqual(str(self.app.parameter_check_button["state"]), "normal")
        self.assertEqual(str(self.app.start_button["state"]), "disabled")
        self._complete_parameter_success()
        self.assertEqual(str(self.app.start_button["state"]), "normal")
        self.app.host_var.set("192.168.1.124")
        self.assertEqual(str(self.app.start_button["state"]), "disabled")

    def test_connection_failure_and_acquisition_error_keep_gate_closed(self) -> None:
        fingerprint = self.app._connection_fingerprint()
        test_id = self.app.connection_gate.begin_test(fingerprint)
        self.app.events.put(
            SessionEvent(
                "connection_test_error",
                message="timeout",
                connection_fingerprint=fingerprint,
                connection_test_id=test_id,
            )
        )
        with patch("realtime_stability.app.messagebox.showerror"):
            self.app._poll_events()
        self.assertEqual(str(self.app.start_button["state"]), "disabled")

        self._complete_success()
        self._complete_parameter_success()
        self.app.events.put(SessionEvent("error", message="connection lost"))
        with patch("realtime_stability.app.messagebox.showerror"):
            self.app._poll_events()
        self.assertEqual(str(self.app.start_button["state"]), "disabled")

    def test_normal_stop_preserves_verified_connection(self) -> None:
        self._complete_success()
        self._complete_parameter_success()
        self.app.events.put(SessionEvent("stopped", sample_count=0))
        self.app._poll_events()
        self.assertEqual(str(self.app.start_button["state"]), "normal")

    def test_direct_start_without_gate_does_not_create_session(self) -> None:
        with patch("realtime_stability.app.messagebox.showwarning"):
            self.app._start()
        self.assertIsNone(self.app.session)

    def test_measurement_parameter_change_only_relocks_start(self) -> None:
        self._complete_success()
        self._complete_parameter_success()
        self.assertEqual(str(self.app.start_button["state"]), "normal")
        self.app.gate_var.set("0.2")
        self.assertEqual(str(self.app.parameter_check_button["state"]), "normal")
        self.assertEqual(str(self.app.start_button["state"]), "disabled")

    def test_parameter_failure_keeps_start_disabled(self) -> None:
        self._complete_success()
        fingerprint = self.app._parameter_fingerprint()
        test_id = self.app.connection_gate.begin_parameter_test(fingerprint)
        self.app.events.put(
            SessionEvent(
                "parameter_test_error",
                message="gate_time_s mismatch",
                parameter_fingerprint=fingerprint,
                connection_test_id=test_id,
            )
        )
        with patch("realtime_stability.app.messagebox.showerror"):
            self.app._poll_events()
        self.assertEqual(str(self.app.parameter_check_button["state"]), "normal")
        self.assertEqual(str(self.app.start_button["state"]), "disabled")


if __name__ == "__main__":
    unittest.main()
