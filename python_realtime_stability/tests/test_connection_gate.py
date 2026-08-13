from __future__ import annotations

import unittest

from realtime_stability.connection_gate import (
    ConnectionGate,
    make_fingerprint,
    make_parameter_fingerprint,
)


class ConnectionGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gate = ConnectionGate()
        self.hardware = make_fingerprint(False, " 192.168.1.123 ", "5025")
        self.other_host = make_fingerprint(False, "192.168.1.124", "5025")
        self.simulation = make_fingerprint(True, "", "5025")
        self.parameters = make_parameter_fingerprint(self.hardware, "2", "0.1", "1 MΩ")

    def test_starts_disabled_until_current_fingerprint_is_verified(self) -> None:
        self.assertFalse(self.gate.is_verified(self.hardware))
        test_id = self.gate.begin_test(self.hardware)
        self.assertTrue(self.gate.testing)
        self.assertFalse(self.gate.is_verified(self.hardware))
        self.assertTrue(self.gate.complete_success(self.hardware, test_id))
        self.assertTrue(self.gate.is_verified(self.hardware))

    def test_failure_keeps_gate_closed(self) -> None:
        test_id = self.gate.begin_test(self.hardware)
        self.assertTrue(self.gate.complete_failure(test_id))
        self.assertFalse(self.gate.is_verified(self.hardware))

    def test_connection_settings_change_invalidates_previous_success(self) -> None:
        test_id = self.gate.begin_test(self.hardware)
        self.gate.complete_success(self.hardware, test_id)
        self.gate.invalidate()
        self.assertFalse(self.gate.is_verified(self.hardware))
        self.assertFalse(self.gate.is_verified(self.other_host))

    def test_stale_result_cannot_reopen_gate_after_new_test(self) -> None:
        old_id = self.gate.begin_test(self.hardware)
        new_id = self.gate.begin_test(self.other_host)
        self.assertFalse(self.gate.complete_success(self.hardware, old_id))
        self.assertFalse(self.gate.is_verified(self.hardware))
        self.assertTrue(self.gate.complete_success(self.other_host, new_id))
        self.assertTrue(self.gate.is_verified(self.other_host))

    def test_simulation_uses_separate_fingerprint(self) -> None:
        test_id = self.gate.begin_test(self.simulation)
        self.gate.complete_success(self.simulation, test_id)
        self.assertTrue(self.gate.is_verified(self.simulation))
        self.assertFalse(self.gate.is_verified(self.hardware))

    def test_parameter_gate_requires_connection_and_parameter_success(self) -> None:
        parameter_id = self.gate.begin_parameter_test(self.parameters)
        self.assertFalse(self.gate.complete_parameter_success(self.parameters, parameter_id))
        connection_id = self.gate.begin_test(self.hardware)
        self.gate.complete_success(self.hardware, connection_id)
        parameter_id = self.gate.begin_parameter_test(self.parameters)
        self.assertTrue(self.gate.complete_parameter_success(self.parameters, parameter_id))
        self.assertTrue(self.gate.is_parameter_verified(self.hardware, self.parameters))

    def test_parameter_change_only_invalidates_parameter_stage(self) -> None:
        connection_id = self.gate.begin_test(self.hardware)
        self.gate.complete_success(self.hardware, connection_id)
        parameter_id = self.gate.begin_parameter_test(self.parameters)
        self.gate.complete_parameter_success(self.parameters, parameter_id)
        self.gate.invalidate_parameters()
        self.assertTrue(self.gate.is_verified(self.hardware))
        self.assertFalse(self.gate.is_parameter_verified(self.hardware, self.parameters))

    def test_retesting_connection_clears_parameter_success(self) -> None:
        connection_id = self.gate.begin_test(self.hardware)
        self.gate.complete_success(self.hardware, connection_id)
        parameter_id = self.gate.begin_parameter_test(self.parameters)
        self.gate.complete_parameter_success(self.parameters, parameter_id)
        self.assertTrue(self.gate.is_parameter_verified(self.hardware, self.parameters))
        self.gate.begin_test(self.hardware)
        self.assertFalse(self.gate.is_parameter_verified(self.hardware, self.parameters))


if __name__ == "__main__":
    unittest.main()
