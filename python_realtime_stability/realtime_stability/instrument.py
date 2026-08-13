from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import io
import re
import socket
import threading
import time

import numpy as np


class InstrumentError(RuntimeError):
    pass


class InstrumentCancelled(InstrumentError):
    """Raised when a pending socket operation is cancelled by the user."""


@dataclass(frozen=True)
class InstrumentIdentity:
    manufacturer: str
    model: str
    serial_number: str
    firmware: str
    raw: str


@dataclass(frozen=True)
class ParameterCheck:
    name: str
    expected: str
    actual: str
    rule: str
    passed: bool


class ParameterValidationError(InstrumentError):
    def __init__(self, checks: list[ParameterCheck], errors: list[str] | None = None) -> None:
        self.checks = checks
        self.errors = errors or []
        failed = [item for item in checks if not item.passed]
        details = [
            f"{item.name}: expected={item.expected}, actual={item.actual}, rule={item.rule}"
            for item in failed
        ]
        details.extend(self.errors)
        super().__init__("parameter validation failed: " + " | ".join(details))


def parse_identity(response: str) -> InstrumentIdentity:
    fields = [item.strip() for item in response.strip().split(",")]
    if len(fields) < 4:
        raise InstrumentError(f"invalid *IDN? response: {response}")
    manufacturer, model, serial_number = fields[:3]
    firmware = ",".join(fields[3:]).strip()
    manufacturer_upper = manufacturer.upper()
    model_upper = model.upper()
    if manufacturer_upper not in {"KEYSIGHT TECHNOLOGIES", "AGILENT TECHNOLOGIES"}:
        raise InstrumentError(f"unexpected instrument manufacturer: {manufacturer}")
    if not model_upper.startswith("53230A"):
        raise InstrumentError(f"connected device is not a 53230A: {response}")
    return InstrumentIdentity(manufacturer, model, serial_number, firmware, response.strip())


@dataclass(frozen=True)
class InstrumentSettings:
    host: str
    port: int = 5025
    channel: int = 2
    gate_time_s: float = 0.1
    impedance_ohm: int = 1_000_000

    def validate(self) -> None:
        if not self.host.strip():
            raise ValueError("IP address is required")
        if not 1 <= self.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if self.channel not in (1, 2):
            raise ValueError("continuous frequency mode supports channel 1 or 2")
        if not 1e-6 <= self.gate_time_s <= 1000.0:
            raise ValueError("gate time must be between 1 us and 1000 s")
        if self.impedance_ohm not in (50, 1_000_000):
            raise ValueError("impedance must be 50 ohm or 1 Mohm")


class Keysight53230A:
    """Minimal raw-SCPI LAN client for continuous 53230A acquisition."""

    MAX_BATCH = 100_000
    OVERFLOW_BIT = 1 << 14
    max_samples_per_segment: int | None = 1_000_000

    def __init__(self, settings: InstrumentSettings) -> None:
        settings.validate()
        self.settings = settings
        self.identity = ""
        self.actual_gate_time_s = settings.gate_time_s
        self._socket: socket.socket | None = None
        self._reader: io.BufferedReader | None = None
        self._cancel_requested = threading.Event()
        self.io_timeout_s = 1.0
        self.identity_fields: InstrumentIdentity | None = None
        self.connection_verified_at_utc: str | None = None
        self.setup_warnings: list[str] = []
        self.setup_errors: list[str] = []
        self.input_diagnostics: dict[str, float | str] = {}
        self.command_log: list[str] = []
        self.operation_condition: int | None = None
        self.io_cancelled = False
        self.parameter_checks: list[ParameterCheck] = []
        self.configuration_verified_at_utc: str | None = None
        self.max_samples_per_segment: int | None = 1_000_000

    def connect(self) -> str:
        self._cancel_requested.clear()
        self.io_cancelled = False
        timeout = max(3.0, min(15.0, self.settings.gate_time_s + 3.0))
        self.io_timeout_s = max(0.5, min(2.0, timeout))
        self._socket = socket.create_connection(
            (self.settings.host, self.settings.port), timeout=timeout
        )
        self._socket.settimeout(self.io_timeout_s)
        self._reader = self._socket.makefile("rb")
        self.identity = self.query_text("*IDN?")
        self.identity_fields = parse_identity(self.identity)
        self.connection_verified_at_utc = datetime.now(timezone.utc).isoformat()
        if self.identity_fields is None:
            self.close()
            raise InstrumentError("instrument identity was not parsed")
        return self.identity

    def test_connection(self) -> InstrumentIdentity:
        self.connect()
        identity = self.identity_fields
        self.close()
        if identity is None:
            raise InstrumentError("instrument identity was not parsed")
        return identity

    def configure(self) -> float:
        self.parameter_checks = []
        self.setup_warnings = []
        self.setup_errors = []
        channel = self.settings.channel
        impedance = 50 if self.settings.impedance_ohm == 50 else "1E6"
        base_commands = [
            "*RST",
            "*CLS",
            "SYST:TIM 9.9E37",
            f"CONF:FREQ (@{channel})",
            "FREQ:MODE CONT",
            "FREQ:GATE:SOUR TIME",
            f"INP{channel}:COUP DC",
            f"INP{channel}:IMP {impedance}",
            "*WAI",
        ]
        for command in base_commands:
            self.write(command)
        base_errors = self.drain_errors()
        if base_errors:
            raise ParameterValidationError(self.parameter_checks, base_errors)
        ptp = self._required_float("INP:LEV:PTP?")
        maximum_level = self._required_float("INP:LEV:MAX?")
        if ptp <= 0:
            raise InstrumentError(f"invalid input peak-to-peak level: {ptp}")
        input_range = 50.0 if ptp > 5.5 else 5.0
        trigger_level = maximum_level - (ptp / 2.0)
        commands = [
            f"INP{channel}:LEV:AUTO ONCE",
            f"INP:RANG {input_range:g}",
            f"INP{channel}:LEV {trigger_level:.15g}",
            f"INP{channel}:SLOP POS",
            f"FREQ:GATE:TIME {self.settings.gate_time_s:.15g}",
            "TRIG:SOUR IMM",
            "SAMP:COUN MAX",
            "TRIG:COUN 1",
            "FORM:DATA REAL,64",
            "FORM:BORD SWAP",
        ]
        for command in commands:
            self.write(command)
        opc_response = self.query_text("*OPC?")
        try:
            opc_value = float(opc_response)
        except ValueError:
            opc_value = float("nan")
        opc_check = ParameterCheck(
            "operation_complete",
            "1",
            opc_response,
            "numeric value must equal exactly 1",
            bool(np.isfinite(opc_value) and opc_value == 1.0),
        )
        self.parameter_checks.append(opc_check)
        if not opc_check.passed:
            raise ParameterValidationError(self.parameter_checks)
        self.setup_errors = self.drain_errors()
        if self.setup_errors:
            raise ParameterValidationError(self.parameter_checks, self.setup_errors)
        self.actual_gate_time_s = self._required_float("FREQ:GATE:TIME?")
        self.parameter_checks.extend(self._readback_checks(channel, impedance, input_range, trigger_level))
        self.collect_input_diagnostics()
        failed = [item for item in self.parameter_checks if not item.passed]
        if failed:
            raise ParameterValidationError(self.parameter_checks)
        self.configuration_verified_at_utc = datetime.now(timezone.utc).isoformat()
        return self.actual_gate_time_s

    def configure_and_validate(self) -> tuple[ParameterCheck, ...]:
        self.configure()
        return tuple(self.parameter_checks)

    def _required_float(self, command: str) -> float:
        response = self.query_text(command)
        try:
            value = float(response)
        except ValueError as exc:
            raise InstrumentError(f"invalid numeric response to {command}: {response}") from exc
        if not np.isfinite(value):
            raise InstrumentError(f"non-finite response to {command}: {response}")
        return value

    @staticmethod
    def _check_text(name: str, expected: str, actual: str, rule: str) -> ParameterCheck:
        normalized_expected = re.sub(r"\s+", "", expected).upper()
        normalized_actual = re.sub(r"\s+", "", actual).upper()
        return ParameterCheck(name, expected, actual, rule, normalized_expected in normalized_actual)

    @staticmethod
    def _check_enum(
        name: str, expected: tuple[str, ...], actual: str, rule: str
    ) -> ParameterCheck:
        normalized_actual = re.sub(r"\s+", "", actual).strip('"').upper()
        normalized_expected = tuple(
            re.sub(r"\s+", "", item).strip('"').upper() for item in expected
        )
        return ParameterCheck(
            name,
            " or ".join(expected),
            actual,
            rule,
            normalized_actual in normalized_expected,
        )

    @staticmethod
    def _check_float(
        name: str, expected: float, actual: float, tolerance: float, rule: str
    ) -> ParameterCheck:
        passed = np.isfinite(actual) and abs(actual - expected) <= tolerance
        return ParameterCheck(
            name,
            format(expected, ".15g"),
            format(actual, ".15g"),
            f"absolute tolerance <= {tolerance:.15g}; {rule}",
            bool(passed),
        )

    def _readback_checks(
        self, channel: int, impedance: str, input_range: float, trigger_level: float
    ) -> list[ParameterCheck]:
        checks: list[ParameterCheck] = []
        checks.append(
            self._check_text(
                "measurement_function",
                "FREQ",
                self.query_text("CONF:FREQ?"),
                "normalized text contains FREQ",
            )
        )
        checks.append(
            self._check_text(
                "measurement_channel",
                f"@{channel}",
                self.query_text("CONF:FREQ?"),
                "normalized text contains selected channel",
            )
        )
        checks.append(self._check_enum("frequency_mode", ("CONT",), self.query_text("FREQ:MODE?"), "normalized enum equals CONT"))
        checks.append(self._check_enum("gate_source", ("TIME",), self.query_text("FREQ:GATE:SOUR?"), "normalized enum equals TIME"))
        checks.append(self._check_float("gate_time_s", self.settings.gate_time_s, self.actual_gate_time_s, max(1e-12, self.settings.gate_time_s * 1e-6), "instrument gate time"))
        checks.append(self._check_enum("input_coupling", ("DC",), self.query_text(f"INP{channel}:COUP?"), "normalized enum equals DC"))
        checks.append(self._check_float("input_impedance_ohm", float(self.settings.impedance_ohm), self._required_float(f"INP{channel}:IMP?"), 1.0, "instrument input impedance"))
        checks.append(self._check_enum("input_slope", ("POS",), self.query_text(f"INP{channel}:SLOP?"), "normalized enum equals POS"))
        checks.append(self._check_enum("trigger_source", ("IMM",), self.query_text("TRIG:SOUR?"), "normalized enum equals IMM"))
        sample_count_response = self.query_text("SAMP:COUN?")
        try:
            sample_count_actual = float(sample_count_response)
        except ValueError:
            checks.append(
                self._check_text(
                    "sample_count", "MAX", sample_count_response, "response is MAX"
                )
            )
        else:
            checks.append(
                ParameterCheck(
                    "sample_count",
                    "MAX (>= 1000000)",
                    sample_count_response,
                    "numeric maximum must be at least the 1000000-point segment limit",
                    bool(np.isfinite(sample_count_actual) and sample_count_actual >= 1_000_000),
                )
            )
        checks.append(self._check_float("trigger_count", 1.0, self._required_float("TRIG:COUN?"), 0.0, "trigger count"))
        checks.append(self._check_enum("data_format", ("REAL,64",), self.query_text("FORM:DATA?"), "normalized enum equals REAL,64"))
        checks.append(self._check_enum("byte_order", ("SWAP",), self.query_text("FORM:BORD?"), "normalized enum equals SWAP"))
        checks.append(self._check_float("instrument_timeout_s", 9.9e37, self._required_float("SYST:TIM?"), 9.9e31, "configured timeout sentinel"))
        checks.append(self._check_float("input_range_v", input_range, self._required_float("INP:RANG?"), 1e-9, "selected range"))
        checks.append(self._check_float("trigger_level_v", trigger_level, self._required_float(f"INP{channel}:LEV?"), max(1e-9, abs(trigger_level) * 1e-6), "computed midpoint trigger level"))
        auto_level = self.query_text(f"INP{channel}:LEV:AUTO?")
        checks.append(self._check_enum("auto_level_state", ("OFF", "0"), auto_level, "AUTO ONCE must finish and leave automatic leveling off"))
        return checks

    def start(self) -> None:
        self.write("INIT")
        errors = self.drain_errors()
        if errors:
            raise InstrumentError("53230A INIT was rejected: " + " | ".join(errors))
        self.operation_condition = int(self.query_float("STAT:OPER:COND?"))

    def read_available(self) -> np.ndarray:
        available = int(self.query_float("DATA:POIN?"))
        if available <= 0:
            return np.empty(0, dtype=np.float64)
        count = min(available, self.MAX_BATCH)
        values = self.query_binary_float64(f"DATA:REM? {count}")
        if values.size != count:
            raise InstrumentError(
                f"requested {count} readings but instrument returned {values.size}"
            )
        return values

    def memory_overflowed(self) -> bool:
        condition = int(self.query_float("STAT:QUES:COND?"))
        return bool(condition & self.OVERFLOW_BIT)

    def read_memory_overflow_event(self) -> bool:
        """Read the latched event and current condition, then clear the event."""
        event = int(self.query_float("STAT:QUES?"))
        condition = int(self.query_float("STAT:QUES:COND?"))
        overflowed = bool((event | condition) & self.OVERFLOW_BIT)
        # STAT:QUES? is a read-to-clear event-register query on this instrument.
        return overflowed

    def drain_errors(self, maximum: int = 100) -> list[str]:
        errors: list[str] = []
        for _ in range(maximum):
            response = self.query_text("SYST:ERR?")
            if self._is_no_error(response):
                return errors
            errors.append(response)
        raise InstrumentError("SYST:ERR? did not reach the no-error response")

    @staticmethod
    def _is_no_error(response: str) -> bool:
        return bool(re.match(r"^\s*\+?0\s*(?:,|$)", response))

    def collect_input_diagnostics(self) -> None:
        channel = self.settings.channel
        optional_queries = {
            "input_range_v": f"INP{channel}:RANG?",
            "input_level_v": f"INP{channel}:LEV?",
            "auto_level_state": f"INP{channel}:LEV:AUTO?",
            "peak_to_peak_v": "INP:LEV:PTP?",
            "maximum_level_v": "INP:LEV:MAX?",
        }
        for name, command in optional_queries.items():
            try:
                response = self.query_text(command)
                try:
                    self.input_diagnostics[name] = float(response)
                except ValueError:
                    self.input_diagnostics[name] = response
            except InstrumentError as exc:
                self.setup_warnings.append(f"{command}: {exc}")
            queued_errors = self.drain_errors()
            if queued_errors:
                self.setup_warnings.extend(f"{command}: {item}" for item in queued_errors)

    def abort(self) -> None:
        if self._socket is not None:
            try:
                self.write("ABOR")
            except (OSError, InstrumentError):
                pass

    def cancel_io(self) -> None:
        self._cancel_requested.set()
        self.io_cancelled = True
        sock = self._socket
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def close(self) -> None:
        reader, sock = self._reader, self._socket
        self._reader = None
        self._socket = None
        if reader is not None:
            try:
                reader.close()
            except OSError:
                pass
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def write(self, command: str) -> None:
        if self._socket is None:
            raise InstrumentError("instrument is not connected")
        try:
            self._socket.sendall(command.encode("ascii") + b"\n")
            self.command_log.append(command)
            if len(self.command_log) > 200:
                del self.command_log[:-200]
        except socket.timeout as exc:
            if self._cancel_requested.is_set():
                raise InstrumentCancelled("instrument I/O cancelled") from exc
            raise InstrumentError(f"timeout sending {command}") from exc
        except OSError as exc:
            if self._cancel_requested.is_set():
                raise InstrumentCancelled("instrument I/O cancelled") from exc
            raise InstrumentError(f"instrument write failed for {command}: {exc}") from exc

    def query_text(self, command: str) -> str:
        self.write(command)
        if self._reader is None:
            raise InstrumentError("instrument is not connected")
        try:
            response = self._reader.readline()
        except socket.timeout as exc:
            if self._cancel_requested.is_set():
                raise InstrumentCancelled("instrument I/O cancelled") from exc
            raise InstrumentError(f"timeout waiting for {command}") from exc
        except OSError as exc:
            if self._cancel_requested.is_set():
                raise InstrumentCancelled("instrument I/O cancelled") from exc
            raise InstrumentError(f"instrument read failed for {command}: {exc}") from exc
        if not response:
            raise InstrumentError(f"connection closed while waiting for {command}")
        return response.decode("ascii", errors="replace").strip()

    def query_float(self, command: str) -> float:
        response = self.query_text(command)
        try:
            return float(response)
        except ValueError as exc:
            raise InstrumentError(f"invalid response to {command}: {response}") from exc

    def query_binary_float64(self, command: str) -> np.ndarray:
        self.write(command)
        if self._reader is None:
            raise InstrumentError("instrument is not connected")
        marker = self._read_exact(1)
        if marker != b"#":
            remainder = self._reader.readline()
            response = (marker + remainder).decode("ascii", errors="replace").strip()
            raise InstrumentError(f"invalid binary response to {command}: {response}")
        digit_count_byte = self._read_exact(1)
        if not digit_count_byte.isdigit() or digit_count_byte == b"0":
            raise InstrumentError("instrument returned an unsupported binary block")
        digit_count = int(digit_count_byte)
        byte_count = int(self._read_exact(digit_count).decode("ascii"))
        if byte_count % 8:
            raise InstrumentError("binary response length is not a multiple of 8")
        payload = self._read_exact(byte_count)
        terminator = self._readline(f"binary response terminator for {command}")
        if terminator not in (b"\n", b"\r\n", b""):
            raise InstrumentError("binary response has an invalid terminator")
        return np.frombuffer(payload, dtype="<f8").copy()

    def _readline(self, operation: str) -> bytes:
        if self._reader is None:
            raise InstrumentError("instrument is not connected")
        try:
            return self._reader.readline()
        except socket.timeout as exc:
            if self._cancel_requested.is_set():
                raise InstrumentCancelled("instrument I/O cancelled") from exc
            raise InstrumentError(f"timeout waiting for {operation}") from exc
        except OSError as exc:
            if self._cancel_requested.is_set():
                raise InstrumentCancelled("instrument I/O cancelled") from exc
            raise InstrumentError(f"instrument read failed for {operation}: {exc}") from exc

    def _read_exact(self, length: int) -> bytes:
        if self._reader is None:
            raise InstrumentError("instrument is not connected")
        try:
            data = self._reader.read(length)
        except socket.timeout as exc:
            if self._cancel_requested.is_set():
                raise InstrumentCancelled("instrument I/O cancelled") from exc
            raise InstrumentError("timeout during instrument binary transfer") from exc
        except OSError as exc:
            if self._cancel_requested.is_set():
                raise InstrumentCancelled("instrument I/O cancelled") from exc
            raise InstrumentError(f"instrument binary read failed: {exc}") from exc
        if data is None or len(data) != length:
            raise InstrumentError("instrument connection ended during data transfer")
        return data


class Simulated53230A:
    """Signal source used to exercise the complete application without hardware."""

    def __init__(self, settings: InstrumentSettings, nominal_hz: float = 10e6) -> None:
        settings.validate()
        self.settings = settings
        self.identity = "SIMULATED,53230A,DEMO,1.0"
        self.actual_gate_time_s = settings.gate_time_s
        self.nominal_hz = nominal_hz
        self._running = False
        self._last_time = 0.0
        self._fractional_walk = 0.0
        self._rng = np.random.default_rng(53230)
        self._lock = threading.Lock()
        self.max_samples_per_segment: int | None = None
        self._segment_read_count = 0
        self.io_cancelled = False
        self.parameter_checks: list[ParameterCheck] = []
        self.configuration_verified_at_utc: str | None = None

    def connect(self) -> str:
        return self.identity

    def configure(self) -> float:
        self.parameter_checks = []
        self.configuration_verified_at_utc = datetime.now(timezone.utc).isoformat()
        return self.actual_gate_time_s

    def configure_and_validate(self) -> tuple[ParameterCheck, ...]:
        self.configure()
        return tuple(self.parameter_checks)

    def start(self) -> None:
        with self._lock:
            self._running = True
            self._last_time = time.monotonic()
            self._segment_read_count = 0

    def read_available(self) -> np.ndarray:
        with self._lock:
            if not self._running:
                return np.empty(0, dtype=np.float64)
            now = time.monotonic()
            count = int((now - self._last_time) / self.actual_gate_time_s)
            count = min(count, 100_000)
            if self.max_samples_per_segment is not None:
                remaining = self.max_samples_per_segment - self._segment_read_count
                count = min(count, remaining)
            if count <= 0:
                return np.empty(0, dtype=np.float64)
            self._last_time += count * self.actual_gate_time_s
            self._segment_read_count += count
        white = self._rng.normal(0.0, 2e-10, count)
        walk_steps = self._rng.normal(0.0, 2e-12, count)
        walk = self._fractional_walk + np.cumsum(walk_steps)
        self._fractional_walk = float(walk[-1])
        return self.nominal_hz * (1.0 + white + walk)

    def memory_overflowed(self) -> bool:
        return False

    def read_memory_overflow_event(self) -> bool:
        return False

    def cancel_io(self) -> None:
        self.abort()

    def abort(self) -> None:
        with self._lock:
            self._running = False

    def close(self) -> None:
        self.abort()
