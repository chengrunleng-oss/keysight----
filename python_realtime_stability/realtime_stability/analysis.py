from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import csv
import os

import numpy as np


@dataclass(frozen=True)
class DeviationResult:
    tau_seconds: np.ndarray
    allan_deviation: np.ndarray
    hadamard_deviation: np.ndarray
    reference_hz: float
    sample_count: int
    window_start_index: int
    continuity_segment: int
    computed_at_utc: str


def octave_averaging_factors(sample_count: int, minimum_terms: int = 8) -> np.ndarray:
    """Return 1, 2, 4, ... factors that support both overlapping deviations."""
    phase_count = sample_count + 1
    max_factor = (phase_count - minimum_terms) // 3
    if max_factor < 1:
        return np.empty(0, dtype=np.int64)
    factors: list[int] = []
    factor = 1
    while factor <= max_factor:
        factors.append(factor)
        factor *= 2
    return np.asarray(factors, dtype=np.int64)


def overlapping_deviations(
    frequency_hz: np.ndarray,
    gate_time_s: float,
    reference_hz: float = 0.0,
    *,
    window_start_index: int = 0,
    continuity_segment: int = 0,
) -> DeviationResult:
    """Calculate overlapping Allan and Hadamard deviation from frequency data.

    This follows the phase-error equations used by the original MATLAB project.
    A reference value of zero selects the mean of the supplied analysis window.
    """
    frequency = np.asarray(frequency_hz, dtype=np.float64)
    if frequency.ndim != 1:
        raise ValueError("frequency_hz must be one-dimensional")
    if gate_time_s <= 0:
        raise ValueError("gate_time_s must be greater than zero")
    if frequency.size and not np.all(np.isfinite(frequency)):
        raise ValueError("frequency data contains a non-finite reading")

    selected_reference = float(reference_hz)
    if selected_reference == 0.0 and frequency.size:
        selected_reference = float(np.mean(frequency))
    if frequency.size and (not np.isfinite(selected_reference) or selected_reference == 0.0):
        raise ValueError("reference frequency must be finite and non-zero")

    factors = octave_averaging_factors(int(frequency.size))
    if not factors.size:
        empty = np.empty(0, dtype=np.float64)
        return DeviationResult(
            empty,
            empty.copy(),
            empty.copy(),
            selected_reference,
            int(frequency.size),
            window_start_index,
            continuity_segment,
            datetime.now(timezone.utc).isoformat(),
        )

    fractional_frequency = (frequency - selected_reference) / selected_reference
    phase_error = np.empty(frequency.size + 1, dtype=np.float64)
    phase_error[0] = 0.0
    np.cumsum(fractional_frequency, out=phase_error[1:])
    phase_error[1:] *= gate_time_s

    taus = factors.astype(np.float64) * gate_time_s
    allan = np.empty(factors.size, dtype=np.float64)
    hadamard = np.empty(factors.size, dtype=np.float64)

    for position, (factor, tau) in enumerate(zip(factors, taus, strict=True)):
        m = int(factor)
        allan_difference = (
            phase_error[2 * m :]
            - 2.0 * phase_error[m:-m]
            + phase_error[: -2 * m]
        )
        hadamard_difference = (
            phase_error[3 * m :]
            - 3.0 * phase_error[2 * m : -m]
            + 3.0 * phase_error[m : -2 * m]
            - phase_error[: -3 * m]
        )
        allan[position] = np.sqrt(
            np.dot(allan_difference, allan_difference)
            / (2.0 * allan_difference.size * tau * tau)
        )
        hadamard[position] = np.sqrt(
            np.dot(hadamard_difference, hadamard_difference)
            / (6.0 * hadamard_difference.size * tau * tau)
        )

    return DeviationResult(
        taus,
        allan,
        hadamard,
        selected_reference,
        int(frequency.size),
        window_start_index,
        continuity_segment,
        datetime.now(timezone.utc).isoformat(),
    )


def write_latest_deviations(path: Path, result: DeviationResult) -> None:
    """Atomically replace the latest live-analysis CSV."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "tau_seconds",
                "overlapping_allan_deviation",
                "overlapping_hadamard_deviation",
                "reference_hz",
                "analysis_sample_count",
                "window_start_index",
                "continuity_segment",
                "computed_at_utc",
            ]
        )
        for tau, allan, hadamard in zip(
            result.tau_seconds,
            result.allan_deviation,
            result.hadamard_deviation,
            strict=True,
        ):
            writer.writerow(
                [
                    f"{tau:.15g}",
                    f"{allan:.15g}",
                    f"{hadamard:.15g}",
                    f"{result.reference_hz:.15g}",
                    result.sample_count,
                    result.window_start_index,
                    result.continuity_segment,
                    result.computed_at_utc,
                ]
            )
    os.replace(temporary, path)
