"""Technical indicator math tests (Phase 16 audit follow-up C): every
indicator must genuinely refuse to report a value before its window has
real data behind it -- no min_periods=1 fabrication -- and every closed-
form edge case (a strictly monotonic series' RSI, Bollinger's middle band
matching the plain SMA) must hold exactly, not approximately by luck.
"""

import numpy as np
import pandas as pd
import pytest

from src.engine.indicators import (
    bollinger_bands,
    exponential_moving_average,
    macd,
    relative_strength_index,
    simple_moving_average,
)


def _ramp(n: int, step: float = 1.0, start: float = 100.0) -> pd.Series:
    idx = pd.bdate_range("2026-01-01", periods=n)
    return pd.Series([start + i * step for i in range(n)], index=idx)


def _flat(n: int, value: float = 100.0) -> pd.Series:
    idx = pd.bdate_range("2026-01-01", periods=n)
    return pd.Series([value] * n, index=idx)


class TestSimpleMovingAverage:
    def test_leading_window_minus_one_points_are_null_not_a_partial_average(self):
        close = _ramp(30)
        sma = simple_moving_average(close, 20)
        assert sma.iloc[:19].isna().all()
        assert not pd.isna(sma.iloc[19])

    def test_first_real_value_matches_hand_computed_mean(self):
        close = _ramp(25)
        sma = simple_moving_average(close, 20)
        assert sma.iloc[19] == pytest.approx(close.iloc[:20].mean())

    def test_flat_series_sma_equals_the_flat_value(self):
        close = _flat(25, value=50.0)
        sma = simple_moving_average(close, 20)
        assert sma.iloc[19:].tolist() == pytest.approx([50.0] * (len(close) - 19))


class TestExponentialMovingAverage:
    def test_leading_span_minus_one_points_are_null(self):
        close = _ramp(30)
        ema = exponential_moving_average(close, 20)
        assert ema.iloc[:19].isna().all()
        assert not pd.isna(ema.iloc[19])

    def test_flat_series_ema_equals_the_flat_value(self):
        close = _flat(30, value=42.0)
        ema = exponential_moving_average(close, 10)
        assert ema.dropna().tolist() == pytest.approx([42.0] * ema.dropna().shape[0])


class TestRelativeStrengthIndex:
    def test_strictly_rising_series_is_genuinely_rsi_100(self):
        close = _ramp(30)
        rsi = relative_strength_index(close, 14)
        assert rsi.iloc[-1] == pytest.approx(100.0)

    def test_strictly_falling_series_is_genuinely_rsi_0(self):
        close = _ramp(30, step=-1.0)
        rsi = relative_strength_index(close, 14)
        assert rsi.iloc[-1] == pytest.approx(0.0)

    def test_flat_series_rsi_is_defined_not_nan_from_a_zero_over_zero(self):
        # avg_gain == avg_loss == 0 for a perfectly flat series -- the
        # `.where(avg_loss != 0, 100.0)` branch must not misfire here;
        # a flat series has no losses, so it falls out to the same
        # "no losses observed" 100.0 case as a strictly-rising series.
        close = _flat(30)
        rsi = relative_strength_index(close, 14)
        assert not rsi.iloc[16:].isna().any()

    def test_leading_points_before_period_observations_are_null(self):
        close = _ramp(30)
        rsi = relative_strength_index(close, 14)
        # diff() drops one point, then ewm needs `period` observations.
        assert rsi.iloc[:14].isna().all()


class TestMacd:
    def test_leading_points_before_slow_ema_warms_up_are_null(self):
        close = _ramp(60)
        macd_line, signal_line, histogram = macd(close, fast=12, slow=26, signal=9)
        assert macd_line.iloc[:25].isna().all()
        assert not pd.isna(macd_line.iloc[25])

    def test_histogram_is_exactly_macd_minus_signal_everywhere_both_are_real(self):
        close = _ramp(60)
        macd_line, signal_line, histogram = macd(close, fast=12, slow=26, signal=9)
        both_real = macd_line.notna() & signal_line.notna()
        assert both_real.any()
        diff = (macd_line - signal_line)[both_real]
        assert histogram[both_real].tolist() == pytest.approx(diff.tolist())

    def test_flat_series_macd_line_is_zero_once_warmed_up(self):
        close = _flat(60)
        macd_line, _signal_line, _histogram = macd(close, fast=12, slow=26, signal=9)
        assert macd_line.dropna().tolist() == pytest.approx(
            [0.0] * macd_line.dropna().shape[0], abs=1e-9
        )


class TestBollingerBands:
    def test_middle_band_equals_plain_sma_exactly(self):
        close = _ramp(30)
        upper, middle, lower = bollinger_bands(close, window=20, num_std=2.0)
        sma = simple_moving_average(close, 20)
        assert middle.dropna().tolist() == pytest.approx(sma.dropna().tolist())

    def test_upper_and_lower_bracket_the_middle_once_real(self):
        close = _ramp(30)
        upper, middle, lower = bollinger_bands(close, window=20, num_std=2.0)
        real = middle.notna()
        assert (upper[real] >= middle[real]).all()
        assert (lower[real] <= middle[real]).all()

    def test_flat_series_has_zero_bandwidth(self):
        close = _flat(30, value=75.0)
        upper, middle, lower = bollinger_bands(close, window=20, num_std=2.0)
        real = middle.notna()
        assert np.allclose(upper[real], lower[real])
        assert np.allclose(upper[real], 75.0)

    def test_leading_window_minus_one_points_are_null(self):
        close = _ramp(30)
        upper, middle, lower = bollinger_bands(close, window=20, num_std=2.0)
        assert upper.iloc[:19].isna().all()
        assert lower.iloc[:19].isna().all()
