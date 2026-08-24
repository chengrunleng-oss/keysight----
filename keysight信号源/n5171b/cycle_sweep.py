"""Round-trip frequency cycles implemented with one N5171B list."""

from __future__ import annotations

from dataclasses import dataclass
import math

from .connection import ScpiConnection
from .list_sweep import (
    DOCUMENTED_MAX_DWELL_S,
    DOCUMENTED_MIN_DWELL_S,
    ListSweepController,
    MAX_LIST_POINTS,
)


@dataclass(frozen=True)
class CycleSweepSettings:
    """Round-trip list parameters accepted by the instrument."""

    f0_mhz: float
    f1_mhz: float
    transition_points: int
    f1_hold_points: int
    trigger_mode: str
    requested_dwell_s: float
    actual_dwell_s: float
    minimum_dwell_s: float

    @property
    def total_points(self) -> int:
        return 2 * self.transition_points + self.f1_hold_points - 2

    @property
    def f1_hold_time_s(self) -> float:
        """Return dwell time spent on all consecutive f1 entries."""
        return self.actual_dwell_s * self.f1_hold_points

    @property
    def programmed_cycle_time_s(self) -> float:
        """Return dwell-only cycle duration; switching time is not included."""
        return self.actual_dwell_s * self.total_points


class CycleSweepController:
    """Configure one f0-f1-f0 list for immediate or repeated TTL cycles."""

    def __init__(
        self, scpi: ScpiConnection, dwell_controller: ListSweepController
    ) -> None:
        self.scpi = scpi
        self._dwell_controller = dwell_controller

    def run(
        self,
        f0_mhz: float,
        f1_mhz: float,
        transition_points: int,
        f1_hold_points: int,
        dwell_s: float,
        trigger_mode: str = "immediate",
        trigger_input: str = "TRIG1",
        edge: str = "POS",
        rf_on: bool = True,
        completion_timeout_s: float | None = None,
    ) -> CycleSweepSettings:
        """Run once immediately or continuously re-arm one round trip per TTL edge."""
        mode = _trigger_mode(trigger_mode)
        if mode == "external":
            trigger_input, edge = _trigger_settings(trigger_input, edge)

        settings = self._configure(
            f0_mhz=f0_mhz,
            f1_mhz=f1_mhz,
            transition_points=transition_points,
            f1_hold_points=f1_hold_points,
            dwell_s=dwell_s,
            trigger_mode=mode,
        )

        self.scpi.write("LIST:TRIG:SOUR IMM")
        if mode == "immediate":
            self.scpi.write("TRIG:SOUR IMM")
            self.scpi.write("INIT:CONT OFF")
        else:
            self.scpi.write(f"TRIG:EXT:SOUR {trigger_input}")
            self.scpi.write(f"TRIG:SLOP {edge}")
            self.scpi.write("TRIG:SOUR EXT")
            self.scpi.write("INIT:CONT OFF")

        self.scpi.write(f"OUTP {'ON' if rf_on else 'OFF'}")
        self.scpi.write("FREQ:MODE LIST")
        self.scpi.write("LIST:MODE AUTO")
        self._raise_if_scpi_error()

        if mode == "external":
            self.scpi.write("INIT:CONT ON")
            self.scpi.write("INIT")
            self._raise_if_scpi_error()
            return settings

        timeout = completion_timeout_s
        if timeout is None:
            timeout = max(
                self.scpi.timeout,
                settings.programmed_cycle_time_s
                + settings.total_points * 0.05
                + 10.0,
            )
        else:
            timeout = _positive_finite("completion_timeout_s", timeout)

        self.scpi.write("INIT")
        if self.scpi.query("*OPC?", timeout=timeout).strip() != "1":
            raise RuntimeError("instrument did not report cycle sweep completion")
        self._raise_if_scpi_error()
        self._assert_ready_for_next_cycle()
        return settings

    def abort(self, rf_off: bool = True) -> None:
        """Stop repeated triggering and return to fixed-frequency mode."""
        commands = ["INIT:CONT OFF", "ABOR", "FREQ:MODE CW"]
        if rf_off:
            commands.append("OUTP OFF")
        self.scpi.write_many(*commands)

    def _configure(
        self,
        f0_mhz: float,
        f1_mhz: float,
        transition_points: int,
        f1_hold_points: int,
        dwell_s: float,
        trigger_mode: str,
    ) -> CycleSweepSettings:
        f0, f1, transition_count, hold_count, requested_dwell = _parameters(
            f0_mhz,
            f1_mhz,
            transition_points,
            f1_hold_points,
            dwell_s,
        )
        total_points = 2 * transition_count + hold_count - 2
        if total_points > MAX_LIST_POINTS:
            raise ValueError(
                "cycle list is too long: "
                f"2 * transition_points + f1_hold_points - 2 must not exceed "
                f"{MAX_LIST_POINTS}"
            )

        self._enter_cw_holding_current_output()
        dwell = self._dwell_controller.set_and_check_dwell(requested_dwell)
        frequencies_hz = _frequencies_hz(f0, f1, transition_count, hold_count)
        frequency_values = ",".join(_number(value) for value in frequencies_hz)
        dwell_values = ",".join(
            _number(dwell.programmed_s) for _ in range(total_points)
        )

        self.scpi.write("POW:MODE FIX")
        self.scpi.write("LIST:TYPE LIST")
        self.scpi.write("LIST:MODE MAN")
        self.scpi.write("LIST:RETR ON")
        self.scpi.write("LIST:DWEL:TYPE LIST")
        self.scpi.write(f"LIST:FREQ {frequency_values}")
        self.scpi.write(f"LIST:DWEL {dwell_values}")
        self.scpi.write("LIST:DIR UP")
        self.scpi.write("LIST:MAN 1")

        actual_dwells = self._query_dwell_values()
        frequency_points = int(float(self.scpi.query("LIST:FREQ:POIN?")))
        dwell_points = int(float(self.scpi.query("LIST:DWEL:POIN?")))
        manual_point = int(float(self.scpi.query("LIST:MAN?")))
        self._raise_if_scpi_error()

        if frequency_points != total_points or dwell_points != total_points:
            raise RuntimeError(
                "instrument did not accept the complete cycle list: "
                f"frequency points={frequency_points}, dwell points={dwell_points}"
            )
        if manual_point != 1:
            raise RuntimeError(
                f"instrument did not select cycle point 1; read back {manual_point}"
            )
        if len(actual_dwells) != total_points or any(
            not math.isclose(
                value, dwell.programmed_s, rel_tol=0.0, abs_tol=1e-12
            )
            for value in actual_dwells
        ):
            raise RuntimeError(
                "instrument cycle dwell readback differs from the programmed value"
            )

        return CycleSweepSettings(
            f0_mhz=f0,
            f1_mhz=f1,
            transition_points=transition_count,
            f1_hold_points=hold_count,
            trigger_mode=trigger_mode,
            requested_dwell_s=requested_dwell,
            actual_dwell_s=actual_dwells[0],
            minimum_dwell_s=dwell.minimum_s,
        )

    def _enter_cw_holding_current_output(self) -> None:
        self.scpi.write("*CLS")
        self.scpi.write("INIT:CONT OFF")
        self.scpi.write("ABOR")
        frequency_mode = self.scpi.query("FREQ:MODE?").strip().upper()

        if frequency_mode == "LIST":
            sweep_type = self.scpi.query("LIST:TYPE?").strip().upper()
            operation_mode = self.scpi.query("LIST:MODE?").strip().upper()
            if sweep_type != "LIST":
                raise RuntimeError(
                    "cannot preserve the current output from a step sweep; "
                    "set a fixed frequency before configuring a cycle sweep"
                )
            point_command = "LIST:CPO?" if operation_mode == "AUTO" else "LIST:MAN?"
            if operation_mode != "AUTO" and not operation_mode.startswith("MAN"):
                raise RuntimeError(
                    f"unexpected LIST:MODE? response: {operation_mode!r}"
                )
            point_response = self.scpi.query(point_command)
            current_point = _integer_response(point_response, point_command)
            frequencies = self._query_frequency_values()
            if not 1 <= current_point <= len(frequencies):
                raise RuntimeError(
                    "current list point is outside the active frequency list: "
                    f"point={current_point}, frequency points={len(frequencies)}"
                )
            hold_frequency_hz = frequencies[current_point - 1]
        elif frequency_mode in {"CW", "FIX", "FIXED"}:
            response = self.scpi.query("FREQ:CW?")
            try:
                hold_frequency_hz = float(response.strip())
            except ValueError as error:
                raise RuntimeError(
                    f"unexpected FREQ:CW? response: {response!r}"
                ) from error
        else:
            raise RuntimeError(
                f"unsupported frequency mode while configuring cycle: {frequency_mode}"
            )

        self._raise_if_scpi_error()
        self.scpi.write(f"FREQ:CW {_number(hold_frequency_hz)}")
        self.scpi.write("FREQ:MODE CW")
        self._raise_if_scpi_error()

    def _assert_ready_for_next_cycle(self) -> None:
        response = self.scpi.query("LIST:CPO?")
        current_point = _integer_response(response, "LIST:CPO?")
        self._raise_if_scpi_error()
        if current_point != 1:
            raise RuntimeError(
                "cycle sweep did not retrace to point 1; "
                f"read back point {current_point}"
            )

    def _query_frequency_values(self) -> list[float]:
        return _float_list_response(self.scpi.query("LIST:FREQ?"), "LIST:FREQ?")

    def _query_dwell_values(self) -> list[float]:
        return _float_list_response(self.scpi.query("LIST:DWEL?"), "LIST:DWEL?")

    def _raise_if_scpi_error(self) -> None:
        response = self.scpi.query("SYST:ERR?").strip()
        code_text = response.split(",", 1)[0]
        try:
            code = int(code_text)
        except ValueError as error:
            raise RuntimeError(f"unexpected SYST:ERR? response: {response!r}") from error
        if code != 0:
            raise RuntimeError(f"instrument SCPI error: {response}")


def _parameters(
    f0_mhz: float,
    f1_mhz: float,
    transition_points: int,
    f1_hold_points: int,
    dwell_s: float,
) -> tuple[float, float, int, int, float]:
    f0 = _positive_finite("f0_mhz", f0_mhz)
    f1 = _positive_finite("f1_mhz", f1_mhz)
    if f0 == f1:
        raise ValueError("f0_mhz and f1_mhz must be different")
    transition_count = _integer_at_least("transition_points", transition_points, 2)
    hold_count = _integer_at_least("f1_hold_points", f1_hold_points, 1)
    dwell = _positive_finite("dwell_s", dwell_s)
    if dwell < DOCUMENTED_MIN_DWELL_S:
        raise ValueError(
            f"dwell_s must be at least {DOCUMENTED_MIN_DWELL_S:.9g} s"
        )
    if dwell > DOCUMENTED_MAX_DWELL_S:
        raise ValueError(
            f"dwell_s must not exceed {DOCUMENTED_MAX_DWELL_S:g} s"
        )
    return f0, f1, transition_count, hold_count, dwell


def _frequencies_hz(
    f0_mhz: float,
    f1_mhz: float,
    transition_points: int,
    f1_hold_points: int,
) -> list[float]:
    upward = _linear_frequencies_hz(f0_mhz, f1_mhz, transition_points)
    downward = _linear_frequencies_hz(f1_mhz, f0_mhz, transition_points)
    repeated_f1 = [f1_mhz * 1_000_000] * (f1_hold_points - 1)
    return upward + repeated_f1 + downward[1:]


def _linear_frequencies_hz(start_mhz: float, stop_mhz: float, points: int) -> list[float]:
    step_mhz = (stop_mhz - start_mhz) / (points - 1)
    frequencies = [
        (start_mhz + index * step_mhz) * 1_000_000 for index in range(points)
    ]
    frequencies[-1] = stop_mhz * 1_000_000
    return frequencies


def _positive_finite(name: str, value: float) -> float:
    converted = float(value)
    if not math.isfinite(converted) or converted <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return converted


def _integer_at_least(name: str, value: int, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def _trigger_mode(trigger_mode: str) -> str:
    if not isinstance(trigger_mode, str):
        raise TypeError("trigger_mode must be a string")
    normalized = trigger_mode.lower()
    if normalized not in {"immediate", "external"}:
        raise ValueError("trigger_mode must be 'immediate' or 'external'")
    return normalized


def _trigger_settings(trigger_input: str, edge: str) -> tuple[str, str]:
    input_name = trigger_input.upper()
    trigger_edge = edge.upper()
    if input_name not in {"TRIG1", "TRIG2", "PULSE"}:
        raise ValueError("trigger_input must be TRIG1, TRIG2, or PULSE")
    if trigger_edge not in {"POS", "NEG"}:
        raise ValueError("edge must be POS or NEG")
    return input_name, trigger_edge


def _float_list_response(response: str, command: str) -> list[float]:
    try:
        return [float(value.strip()) for value in response.split(",")]
    except ValueError as error:
        raise RuntimeError(f"unexpected {command} response: {response!r}") from error


def _integer_response(response: str, command: str) -> int:
    try:
        return int(float(response.strip()))
    except ValueError as error:
        raise RuntimeError(f"unexpected {command} response: {response!r}") from error


def _number(value: float) -> str:
    return format(float(value), ".12g")
