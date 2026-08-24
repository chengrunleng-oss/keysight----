"""Linear frequency sweeps implemented with the N5171B list subsystem."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
import math

from .connection import ScpiConnection


DOCUMENTED_MIN_DWELL_S = 100e-6
DOCUMENTED_MAX_DWELL_S = 100.0
DWELL_RESOLUTION_S = 1e-6
MAX_LIST_POINTS = 3201


@dataclass(frozen=True)
class DwellSetting:
    """Result of programming and reading back a list dwell value."""

    requested_s: float
    programmed_s: float
    actual_s: float
    minimum_s: float

    @property
    def exact(self) -> bool:
        return math.isclose(
            self.requested_s, self.actual_s, rel_tol=0.0, abs_tol=1e-12
        )


@dataclass(frozen=True)
class LinearSweepSettings:
    """List parameters accepted by the instrument."""

    start_mhz: float
    stop_mhz: float
    points: int
    direction: str
    requested_sweep_time_s: float
    requested_dwell_s: float
    actual_dwell_s: float
    minimum_dwell_s: float

    @property
    def programmed_dwell_time_s(self) -> float:
        """Return the dwell-only duration; switching time is not included."""
        return self.actual_dwell_s * self.points

    @property
    def output_start_mhz(self) -> float:
        """Return the first frequency emitted in the selected direction."""
        return self.start_mhz if self.direction == "forward" else self.stop_mhz

    @property
    def output_stop_mhz(self) -> float:
        """Return the final frequency emitted in the selected direction."""
        return self.stop_mhz if self.direction == "forward" else self.start_mhz


class ListSweepController:
    """Configure and execute single linear list sweeps."""

    def __init__(self, scpi: ScpiConnection) -> None:
        self.scpi = scpi

    def measure_minimum_dwell(self) -> float:
        """Program the documented 100 us minimum and return its readback value."""
        result = self._set_and_read_back_dwell(
            requested_s=DOCUMENTED_MIN_DWELL_S,
            minimum_s=DOCUMENTED_MIN_DWELL_S,
            points=1,
        )
        return result.actual_s

    def set_and_check_dwell(self, dwell_s: float) -> DwellSetting:
        """Set one list dwell value and verify the value reported by the instrument."""
        requested = _positive_finite("dwell_s", dwell_s)
        minimum = self.measure_minimum_dwell()
        if requested < minimum:
            raise ValueError(
                f"dwell_s must be at least the measured minimum {minimum:.9g} s"
            )
        if requested > DOCUMENTED_MAX_DWELL_S:
            raise ValueError(
                f"dwell_s must not exceed {DOCUMENTED_MAX_DWELL_S:g} s"
            )

        return self._set_and_read_back_dwell(
            requested_s=requested,
            minimum_s=minimum,
            points=1,
        )

    def run_linear_sweep(
        self,
        start_mhz: float,
        stop_mhz: float,
        points: int,
        sweep_time_s: float,
        direction: str = "forward",
        rf_on: bool = True,
        completion_timeout_s: float | None = None,
    ) -> LinearSweepSettings:
        """Run one sweep immediately and return after the instrument completes it."""
        settings = self._configure_linear_sweep(
            start_mhz=start_mhz,
            stop_mhz=stop_mhz,
            points=points,
            sweep_time_s=sweep_time_s,
            direction=direction,
        )
        self.scpi.write("LIST:TRIG:SOUR IMM")
        self.scpi.write("TRIG:SOUR IMM")
        self.scpi.write("INIT:CONT OFF")
        self.scpi.write(f"OUTP {'ON' if rf_on else 'OFF'}")
        self.scpi.write("FREQ:MODE LIST")
        self._raise_if_scpi_error()

        timeout = completion_timeout_s
        if timeout is None:
            timeout = max(
                self.scpi.timeout,
                settings.programmed_dwell_time_s + settings.points * 0.05 + 10.0,
            )
        else:
            timeout = _positive_finite("completion_timeout_s", timeout)

        self.scpi.write("INIT")
        if self.scpi.query("*OPC?", timeout=timeout).strip() != "1":
            raise RuntimeError("instrument did not report sweep completion")
        self._raise_if_scpi_error()
        self._assert_completed_endpoint(settings)
        return settings

    def arm_linear_sweep_for_trigger(
        self,
        start_mhz: float,
        stop_mhz: float,
        points: int,
        sweep_time_s: float,
        direction: str = "forward",
        trigger_input: str = "TRIG1",
        edge: str = "POS",
        rf_on: bool = True,
    ) -> LinearSweepSettings:
        """Arm one sweep; one external edge starts the complete frequency list."""
        trigger_input, edge = _trigger_settings(trigger_input, edge)
        settings = self._configure_linear_sweep(
            start_mhz=start_mhz,
            stop_mhz=stop_mhz,
            points=points,
            sweep_time_s=sweep_time_s,
            direction=direction,
        )
        self.scpi.write("LIST:TRIG:SOUR IMM")
        self.scpi.write(f"TRIG:EXT:SOUR {trigger_input}")
        self.scpi.write(f"TRIG:SLOP {edge}")
        self.scpi.write("TRIG:SOUR EXT")
        self.scpi.write("INIT:CONT OFF")
        self.scpi.write(f"OUTP {'ON' if rf_on else 'OFF'}")
        self.scpi.write("FREQ:MODE LIST")
        self._raise_if_scpi_error()
        self.scpi.write("INIT")
        self._raise_if_scpi_error()
        return settings

    def abort(self, rf_off: bool = True) -> None:
        """Abort a running or armed sweep and return to fixed-frequency mode."""
        commands = ["INIT:CONT OFF", "ABOR", "FREQ:MODE CW"]
        if rf_off:
            commands.append("OUTP OFF")
        self.scpi.write_many(*commands)

    def _configure_linear_sweep(
        self,
        start_mhz: float,
        stop_mhz: float,
        points: int,
        sweep_time_s: float,
        direction: str,
    ) -> LinearSweepSettings:
        start, stop, point_count, sweep_time = _linear_parameters(
            start_mhz, stop_mhz, points, sweep_time_s
        )
        sweep_direction, scpi_direction = _sweep_direction(direction)

        self.scpi.write("*CLS")
        self.scpi.write("INIT:CONT OFF")
        self.scpi.write("ABOR")
        hold_frequency_hz = self._assert_ready_endpoint(
            sweep_direction, point_count
        )

        # Keep the old endpoint on RF while the active list is being replaced.
        self.scpi.write(f"FREQ:CW {_number(hold_frequency_hz)}")
        self.scpi.write("FREQ:MODE CW")
        self._raise_if_scpi_error()

        minimum = self.measure_minimum_dwell()
        requested_dwell = sweep_time / point_count
        if requested_dwell < minimum:
            minimum_time = minimum * point_count
            raise ValueError(
                "sweep_time_s is too short: "
                f"{point_count} points require at least {minimum_time:.9g} s "
                "of total dwell time"
            )
        if requested_dwell > DOCUMENTED_MAX_DWELL_S:
            raise ValueError(
                "sweep_time_s is too long: dwell at each point must not exceed "
                f"{DOCUMENTED_MAX_DWELL_S:g} s"
            )

        programmed_dwell = _quantize_dwell(requested_dwell)
        frequencies_hz = _linear_frequencies_hz(start, stop, point_count)
        frequency_values = ",".join(_number(value) for value in frequencies_hz)
        dwell_values = ",".join(
            _number(programmed_dwell) for _ in range(point_count)
        )

        # Keep these writes separate so each hardware transition can be observed.
        self.scpi.write("POW:MODE FIX")
        self.scpi.write("LIST:TYPE LIST")
        self.scpi.write("LIST:MODE AUTO")
        self.scpi.write("LIST:RETR OFF")
        self.scpi.write("LIST:DWEL:TYPE LIST")
        self.scpi.write(f"LIST:FREQ {frequency_values}")
        self.scpi.write(f"LIST:DWEL {dwell_values}")
        self.scpi.write(f"LIST:DIR {scpi_direction}")

        actual_dwells = self._query_dwell_values()
        frequency_points = int(float(self.scpi.query("LIST:FREQ:POIN?")))
        dwell_points = int(float(self.scpi.query("LIST:DWEL:POIN?")))
        current_point = self._query_list_point()
        self._raise_if_scpi_error()

        if frequency_points != point_count or dwell_points != point_count:
            raise RuntimeError(
                "instrument did not accept the complete list: "
                f"frequency points={frequency_points}, dwell points={dwell_points}"
            )
        expected_point = _starting_point(sweep_direction, point_count)
        if current_point != expected_point:
            raise RuntimeError(
                "instrument changed the AUTO sweep point while loading the list: "
                f"expected {expected_point}, read back {current_point}"
            )
        if len(actual_dwells) != point_count or any(
            not math.isclose(
                value, programmed_dwell, rel_tol=0.0, abs_tol=1e-12
            )
            for value in actual_dwells
        ):
            raise RuntimeError(
                "instrument dwell readback differs from the programmed value"
            )

        return LinearSweepSettings(
            start_mhz=start,
            stop_mhz=stop,
            points=point_count,
            direction=sweep_direction,
            requested_sweep_time_s=sweep_time,
            requested_dwell_s=requested_dwell,
            actual_dwell_s=actual_dwells[0],
            minimum_dwell_s=minimum,
        )

    def _set_and_read_back_dwell(
        self,
        requested_s: float,
        minimum_s: float,
        points: int,
    ) -> DwellSetting:
        programmed = _quantize_dwell(requested_s)
        values = ",".join(_number(programmed) for _ in range(points))

        self.scpi.write("*CLS")
        self.scpi.write("LIST:DWEL:TYPE LIST")
        self.scpi.write(f"LIST:DWEL {values}")
        actual_values = self._query_dwell_values()
        self._raise_if_scpi_error()

        if len(actual_values) != points or any(
            not math.isclose(value, programmed, rel_tol=0.0, abs_tol=1e-12)
            for value in actual_values
        ):
            raise RuntimeError(
                f"instrument cannot set list dwell to {programmed:.9g} s"
            )

        return DwellSetting(
            requested_s=requested_s,
            programmed_s=programmed,
            actual_s=actual_values[0],
            minimum_s=minimum_s,
        )

    def _query_dwell_values(self) -> list[float]:
        response = self.scpi.query("LIST:DWEL?")
        try:
            return [float(value.strip()) for value in response.split(",")]
        except ValueError as error:
            raise RuntimeError(f"unexpected LIST:DWEL? response: {response!r}") from error

    def _assert_ready_endpoint(self, direction: str, points: int) -> float:
        """Validate the AUTO endpoint and return its frequency in hertz."""
        sweep_type = self.scpi.query("LIST:TYPE?").strip().upper()
        operation_mode = self.scpi.query("LIST:MODE?").strip().upper()
        current_point = self._query_list_point()
        self._raise_if_scpi_error()

        if sweep_type != "LIST" or operation_mode != "AUTO":
            raise RuntimeError(
                "alternating sweeps require LIST:TYPE LIST and LIST:MODE AUTO; "
                f"read back type={sweep_type}, mode={operation_mode}"
            )

        expected_point = _starting_point(direction, points)
        if current_point != expected_point:
            raise RuntimeError(
                f"{direction} sweep requires AUTO point {expected_point} before "
                f"the list is changed; current point is {current_point}"
            )

        frequencies = self._query_frequency_values()
        self._raise_if_scpi_error()
        if current_point > len(frequencies):
            raise RuntimeError(
                "current AUTO point is outside the active frequency list: "
                f"point={current_point}, frequency points={len(frequencies)}"
            )
        return frequencies[current_point - 1]

    def _query_frequency_values(self) -> list[float]:
        response = self.scpi.query("LIST:FREQ?")
        try:
            return [float(value.strip()) for value in response.split(",")]
        except ValueError as error:
            raise RuntimeError(
                f"unexpected LIST:FREQ? response: {response!r}"
            ) from error

    def _assert_completed_endpoint(self, settings: LinearSweepSettings) -> None:
        expected_point = _ending_point(settings.direction, settings.points)
        current_point = self._query_list_point()
        self._raise_if_scpi_error()
        if current_point != expected_point:
            raise RuntimeError(
                "sweep completed at an unexpected AUTO point: "
                f"expected {expected_point}, read back {current_point}"
            )

    def _query_list_point(self) -> int:
        response = self.scpi.query("LIST:CPO?").strip()
        try:
            return int(float(response))
        except ValueError as error:
            raise RuntimeError(f"unexpected LIST:CPO? response: {response!r}") from error

    def _raise_if_scpi_error(self) -> None:
        response = self.scpi.query("SYST:ERR?").strip()
        code_text = response.split(",", 1)[0]
        try:
            code = int(code_text)
        except ValueError as error:
            raise RuntimeError(f"unexpected SYST:ERR? response: {response!r}") from error
        if code != 0:
            raise RuntimeError(f"instrument SCPI error: {response}")


def _linear_parameters(
    start_mhz: float,
    stop_mhz: float,
    points: int,
    sweep_time_s: float,
) -> tuple[float, float, int, float]:
    start = _positive_finite("start_mhz", start_mhz)
    stop = _positive_finite("stop_mhz", stop_mhz)
    if start == stop:
        raise ValueError("start_mhz and stop_mhz must be different")
    if isinstance(points, bool) or not isinstance(points, int):
        raise TypeError("points must be an integer")
    if not 2 <= points <= MAX_LIST_POINTS:
        raise ValueError(f"points must be between 2 and {MAX_LIST_POINTS}")
    sweep_time = _positive_finite("sweep_time_s", sweep_time_s)
    return start, stop, points, sweep_time


def _linear_frequencies_hz(
    start_mhz: float, stop_mhz: float, points: int
) -> list[float]:
    step_mhz = (stop_mhz - start_mhz) / (points - 1)
    frequencies = [
        (start_mhz + index * step_mhz) * 1_000_000 for index in range(points)
    ]
    frequencies[-1] = stop_mhz * 1_000_000
    return frequencies


def _quantize_dwell(dwell_s: float) -> float:
    resolution = Decimal("0.000001")
    steps = (Decimal(str(dwell_s)) / resolution).to_integral_value(
        rounding=ROUND_CEILING
    )
    programmed = float(steps * resolution)
    return max(DOCUMENTED_MIN_DWELL_S, programmed)


def _positive_finite(name: str, value: float) -> float:
    converted = float(value)
    if not math.isfinite(converted) or converted <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return converted


def _trigger_settings(trigger_input: str, edge: str) -> tuple[str, str]:
    input_name = trigger_input.upper()
    trigger_edge = edge.upper()
    if input_name not in {"TRIG1", "TRIG2", "PULSE"}:
        raise ValueError("trigger_input must be TRIG1, TRIG2, or PULSE")
    if trigger_edge not in {"POS", "NEG"}:
        raise ValueError("edge must be POS or NEG")
    return input_name, trigger_edge


def _sweep_direction(direction: str) -> tuple[str, str]:
    if not isinstance(direction, str):
        raise TypeError("direction must be a string")
    normalized = direction.lower()
    if normalized == "forward":
        return normalized, "UP"
    if normalized == "reverse":
        return normalized, "DOWN"
    raise ValueError("direction must be 'forward' or 'reverse'")


def _starting_point(direction: str, points: int) -> int:
    return 1 if direction == "forward" else points


def _ending_point(direction: str, points: int) -> int:
    return points if direction == "forward" else 1


def _number(value: float) -> str:
    return format(float(value), ".12g")
