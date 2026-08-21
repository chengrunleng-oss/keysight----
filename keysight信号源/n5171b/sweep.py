"""Stepped sweep configuration and trigger control."""

from .connection import ScpiConnection


class SweepController:
    """Configure, trigger, start and stop stepped frequency sweeps."""

    def __init__(self, scpi: ScpiConnection) -> None:
        self.scpi = scpi

    def configure_step_sweep(
        self,
        start_mhz: float,
        stop_mhz: float,
        points: int,
        dwell_ms: float,
        power_dbm: float,
        spacing: str = "LIN",
    ) -> None:
        """Configure a stepped sweep and leave RF output disabled."""
        if float(start_mhz) >= float(stop_mhz):
            raise ValueError("start_mhz must be lower than stop_mhz")
        if isinstance(points, bool) or not isinstance(points, int) or points < 2:
            raise ValueError("points must be an integer of at least 2")
        if float(dwell_ms) <= 0:
            raise ValueError("dwell_ms must be greater than 0")

        spacing = spacing.upper()
        if spacing not in {"LIN", "LOG"}:
            raise ValueError("spacing must be LIN or LOG")

        dwell_seconds = float(dwell_ms) / 1000
        self.scpi.write_many(
            "ABOR",
            "OUTP OFF",
            "FREQ:MODE LIST",
            "POW:MODE FIX",
            "LIST:TYPE STEP",
            "LIST:MODE AUTO",
            f"FREQ:STAR {_number(start_mhz)} MHZ",
            f"FREQ:STOP {_number(stop_mhz)} MHZ",
            f"SWE:POIN {points}",
            f"SWE:SPAC {spacing}",
            f"SWE:DWEL {_number(dwell_seconds)} S",
            f"POW {_number(power_dbm)} DBM",
        )

    def use_internal_trigger(self, continuous: bool = True) -> None:
        """Start the configured sweep immediately after arm()."""
        self.scpi.write_many(
            "LIST:TRIG:SOUR IMM",
            "TRIG:SOUR IMM",
            f"INIT:CONT {'ON' if continuous else 'OFF'}",
        )

    def use_ttl_sweep_trigger(
        self, input_name: str = "TRIG1", edge: str = "POS", continuous: bool = True
    ) -> None:
        """Use one rear-panel TTL edge to start each complete sweep."""
        input_name, edge = _ttl_settings(input_name, edge)
        self.scpi.write_many(
            "LIST:TRIG:SOUR IMM",
            f"TRIG:EXT:SOUR {input_name}",
            f"TRIG:SLOP {edge}",
            "TRIG:SOUR EXT",
            f"INIT:CONT {'ON' if continuous else 'OFF'}",
        )

    def use_ttl_point_trigger(
        self, input_name: str = "TRIG1", edge: str = "POS", continuous: bool = True
    ) -> None:
        """Use one rear-panel TTL edge to advance each sweep point."""
        input_name, edge = _ttl_settings(input_name, edge)
        self.scpi.write_many(
            "TRIG:SOUR IMM",
            f"LIST:TRIG:EXT:SOUR {input_name}",
            f"LIST:TRIG:SLOP {edge}",
            "LIST:TRIG:SOUR EXT",
            f"INIT:CONT {'ON' if continuous else 'OFF'}",
        )

    def arm(self, rf_on: bool = True) -> None:
        """Enable RF if requested, then arm the configured sweep."""
        if rf_on:
            self.scpi.write("OUTP ON")
        self.scpi.write("INIT")

    def stop(self) -> None:
        """Abort the sweep and disable RF output."""
        self.scpi.write_many("ABOR", "OUTP OFF")


def _number(value: float) -> str:
    return format(float(value), ".12g")


def _ttl_settings(input_name: str, edge: str) -> tuple[str, str]:
    input_name = input_name.upper()
    edge = edge.upper()
    if input_name not in {"TRIG1", "TRIG2"}:
        raise ValueError("input_name must be TRIG1 or TRIG2")
    if edge not in {"POS", "NEG"}:
        raise ValueError("edge must be POS or NEG")
    return input_name, edge
