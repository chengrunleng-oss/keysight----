"""Fixed-frequency and RF output control."""

from .connection import ScpiConnection


class OutputController:
    """Control continuous-wave settings and the RF output switch."""

    def __init__(self, scpi: ScpiConnection) -> None:
        self.scpi = scpi

    def set_cw(
        self, frequency_mhz: float, power_dbm: float, rf_on: bool = True
    ) -> None:
        """Backward-compatible name for set_single_point()."""
        self.set_single_point(frequency_mhz, power_dbm, rf_on)

    def set_single_point(
        self, frequency_mhz: float, power_dbm: float, rf_on: bool = True
    ) -> None:
        """Set one fixed frequency and power level."""
        self.scpi.write_many(
            "ABOR",
            "OUTP OFF",
            "FREQ:MODE CW",
            "POW:MODE FIX",
            f"FREQ {_number(frequency_mhz)} MHZ",
            f"POW {_number(power_dbm)} DBM",
            f"OUTP {'ON' if rf_on else 'OFF'}",
        )

    def set_frequency(self, frequency_mhz: float) -> None:
        """Stop any sweep and set the fixed output frequency in MHz."""
        self.scpi.write_many(
            "ABOR",
            "FREQ:MODE CW",
            f"FREQ {_number(frequency_mhz)} MHZ",
        )

    def set_power(self, power_dbm: float) -> None:
        """Set the fixed output power level in dBm."""
        self.scpi.write_many(
            "POW:MODE FIX",
            f"POW {_number(power_dbm)} DBM",
        )

    def set_rf(self, enabled: bool) -> None:
        """Enable or disable the RF output."""
        self.scpi.write(f"OUTP {'ON' if enabled else 'OFF'}")

    def get_frequency_mhz(self) -> float:
        """Return the current output frequency in MHz."""
        return float(self.scpi.query("FREQ?")) / 1_000_000

    def get_power_dbm(self) -> float:
        """Return the current output power setting in dBm."""
        return float(self.scpi.query("POW?"))

    def get_rf_enabled(self) -> bool:
        """Return whether the RF output is enabled."""
        return _boolean(self.scpi.query("OUTP?"))

    def get_modulation_enabled(self) -> bool:
        """Return whether RF output modulation is enabled."""
        return _boolean(self.scpi.query("OUTP:MOD?"))

    def get_state(self) -> dict[str, float | bool | str]:
        """Return the current settings of the N5171B RF output channel."""
        return {
            "frequency_mhz": self.get_frequency_mhz(),
            "power_dbm": self.get_power_dbm(),
            "rf_enabled": self.get_rf_enabled(),
            "modulation_enabled": self.get_modulation_enabled(),
            "frequency_mode": self.scpi.query("FREQ:MODE?"),
            "power_mode": self.scpi.query("POW:MODE?"),
        }


def _number(value: float) -> str:
    return format(float(value), ".12g")


def _boolean(response: str) -> bool:
    value = response.strip().upper()
    if value in {"1", "ON"}:
        return True
    if value in {"0", "OFF"}:
        return False
    raise ValueError(f"unexpected boolean response: {response!r}")
