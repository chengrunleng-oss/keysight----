from __future__ import annotations

import unittest

import numpy as np

from realtime_stability.analysis import overlapping_deviations
from realtime_stability.acquisition import RecentSampleBuffer


def matlab_style_deviations(
    frequency: np.ndarray, gate: float, reference: float, factor: int
) -> tuple[float, float]:
    fractional = (frequency - reference) / reference
    phase = np.zeros(frequency.size + 1)
    phase[1:] = np.cumsum(fractional) * gate
    tau = factor * gate
    allan_sum = 0.0
    for index in range(phase.size - 2 * factor):
        value = phase[index + 2 * factor] - 2 * phase[index + factor] + phase[index]
        allan_sum += value * value
    allan = np.sqrt(allan_sum / (2 * (phase.size - 2 * factor) * tau**2))
    hadamard_sum = 0.0
    for index in range(phase.size - 3 * factor):
        value = (
            phase[index + 3 * factor]
            - 3 * phase[index + 2 * factor]
            + 3 * phase[index + factor]
            - phase[index]
        )
        hadamard_sum += value * value
    hadamard = np.sqrt(hadamard_sum / (6 * (phase.size - 3 * factor) * tau**2))
    return allan, hadamard


class DeviationTests(unittest.TestCase):
    def test_constant_frequency_has_zero_deviation(self) -> None:
        result = overlapping_deviations(np.full(200, 10e6), 0.1, 10e6)
        np.testing.assert_array_equal(result.allan_deviation, 0.0)
        np.testing.assert_array_equal(result.hadamard_deviation, 0.0)

    def test_matches_original_matlab_phase_equations(self) -> None:
        rng = np.random.default_rng(7)
        frequency = 10e6 * (1 + rng.normal(0, 1e-9, 500))
        result = overlapping_deviations(frequency, 0.2, 10e6)
        factors = np.rint(result.tau_seconds / 0.2).astype(int)
        for index, factor in enumerate(factors):
            expected_allan, expected_hadamard = matlab_style_deviations(
                frequency, 0.2, 10e6, int(factor)
            )
            self.assertAlmostEqual(result.allan_deviation[index], expected_allan, places=18)
            self.assertAlmostEqual(result.hadamard_deviation[index], expected_hadamard, places=18)

    def test_zero_reference_uses_window_mean(self) -> None:
        frequency = np.linspace(9_999_999.0, 10_000_001.0, 200)
        result = overlapping_deviations(frequency, 1.0, 0.0)
        self.assertAlmostEqual(result.reference_hz, float(np.mean(frequency)))


class RecentBufferTests(unittest.TestCase):
    def test_buffer_retains_latest_points_and_absolute_index(self) -> None:
        buffer = RecentSampleBuffer(5)
        buffer.append(np.asarray([1.0, 2.0, 3.0]))
        buffer.append(np.asarray([4.0, 5.0, 6.0, 7.0]))
        values, start, total, segment = buffer.snapshot()
        np.testing.assert_array_equal(values, [3.0, 4.0, 5.0, 6.0, 7.0])
        self.assertEqual(start, 2)
        self.assertEqual(total, 7)
        self.assertEqual(segment, 0)

    def test_new_segment_clears_values_but_preserves_global_count(self) -> None:
        buffer = RecentSampleBuffer(10)
        buffer.append(np.asarray([1.0, 2.0, 3.0]))
        buffer.start_new_segment()
        buffer.append(np.asarray([4.0, 5.0]))
        values, start, total, segment = buffer.snapshot()
        np.testing.assert_array_equal(values, [4.0, 5.0])
        self.assertEqual(start, 3)
        self.assertEqual(total, 5)
        self.assertEqual(segment, 1)


if __name__ == "__main__":
    unittest.main()
