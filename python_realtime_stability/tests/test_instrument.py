from __future__ import annotations

import unittest

from realtime_stability.instrument import (
    InstrumentError,
    InstrumentSettings,
    Keysight53230A,
    ParameterValidationError,
    parse_identity,
)


class Scripted53230A(Keysight53230A):
    def __init__(self, settings, overrides=None):
        super().__init__(settings)
        self._socket = object()
        self.overrides = overrides or {}
        self.writes = []

    def write(self, command: str) -> None:
        self.writes.append(command)

    def query_text(self, command: str) -> str:
        defaults = {
            "SYST:ERR?": '+0,"No error"',
            "INP:LEV:PTP?": "2.0",
            "INP:LEV:MAX?": "2.0",
            "*OPC?": "1",
            "FREQ:GATE:TIME?": "0.1",
            "CONF:FREQ?": '"FREQ",(@2)',
            "FREQ:MODE?": "CONT",
            "FREQ:GATE:SOUR?": "TIME",
            "INP2:COUP?": "DC",
            "INP2:IMP?": "1000000",
            "INP2:SLOP?": "POS",
            "TRIG:SOUR?": "IMM",
            "SAMP:COUN?": "MAX",
            "TRIG:COUN?": "1",
            "FORM:DATA?": "REAL,64",
            "FORM:BORD?": "SWAP",
            "SYST:TIM?": "9.9E37",
            "INP:RANG?": "5",
            "INP2:RANG?": "5",
            "INP2:LEV?": "1",
            "INP2:LEV:AUTO?": "OFF",
            "INP:LEV:MAX?": "2",
        }
        return self.overrides.get(command, defaults[command])


class IdentityTests(unittest.TestCase):
    def test_parses_exact_53230a_identity(self) -> None:
        identity = parse_identity("KEYSIGHT TECHNOLOGIES,53230A,MY123456,5.1-2.0")
        self.assertEqual(identity.manufacturer, "KEYSIGHT TECHNOLOGIES")
        self.assertEqual(identity.model, "53230A")
        self.assertEqual(identity.serial_number, "MY123456")
        self.assertEqual(identity.firmware, "5.1-2.0")

    def test_rejects_unexpected_model_or_manufacturer(self) -> None:
        with self.assertRaises(InstrumentError):
            parse_identity("KEYSIGHT TECHNOLOGIES,34465A,MY123456,5.1")
        with self.assertRaises(InstrumentError):
            parse_identity("ACME,53230A,MY123456,5.1")
        with self.assertRaises(InstrumentError):
            parse_identity("KEYSIGHT TECHNOLOGIES,53230A")

    def test_hardware_settings_keep_blank_host_invalid(self) -> None:
        with self.assertRaises(ValueError):
            InstrumentSettings(host="").validate()


class ParameterValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = InstrumentSettings(host="test", gate_time_s=0.1)

    def test_all_required_readbacks_return_structured_results(self) -> None:
        source = Scripted53230A(self.settings)
        checks = source.configure_and_validate()
        self.assertGreaterEqual(len(checks), 16)
        self.assertTrue(all(item.passed for item in checks))
        names = {item.name for item in checks}
        self.assertIn("operation_complete", names)
        self.assertIn("gate_time_s", names)
        self.assertIn("trigger_level_v", names)

    def test_opc_other_than_one_fails(self) -> None:
        source = Scripted53230A(self.settings, {"*OPC?": "0"})
        with self.assertRaises(ParameterValidationError) as caught:
            source.configure_and_validate()
        self.assertIn("operation_complete", str(caught.exception))

    def test_single_readback_mismatch_reports_expected_and_actual(self) -> None:
        source = Scripted53230A(self.settings, {"TRIG:SOUR?": "EXT"})
        with self.assertRaises(ParameterValidationError) as caught:
            source.configure_and_validate()
        message = str(caught.exception)
        self.assertIn("trigger_source", message)
        self.assertIn("expected=IMM", message)
        self.assertIn("actual=EXT", message)


if __name__ == "__main__":
    unittest.main()
