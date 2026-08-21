"""Fixed-frequency and RF output control."""

from .connection import ScpiConnection


class OutputController:
    """Control continuous-wave settings and the RF output switch."""

    def __init__(self, scpi: ScpiConnection) -> None:
        self.scpi = scpi

    def set_cw(
        self, frequency_mhz: float, power_dbm: float, rf_on: bool = True
    ) -> None:
        """Set a fixed-frequency output."""
        self.scpi.write_many(
            "ABOR",
            "OUTP OFF",
            "FREQ:MODE CW",
            "POW:MODE FIX",
            f"FREQ {_number(frequency_mhz)} MHZ",
            f"POW {_number(power_dbm)} DBM",
            f"OUTP {'ON' if rf_on else 'OFF'}",
        )

    def set_rf(self, enabled: bool) -> None:
        """Enable or disable the RF output."""
        self.scpi.write(f"OUTP {'ON' if enabled else 'OFF'}")


def _number(value: float) -> str:
    return format(float(value), ".12g")
