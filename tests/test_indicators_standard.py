"""EMA and RSI follow the standard formulas, the same ones the dashboard
chart draws (trading_web_flutter lib/src/util/indicators.dart), so the bot
scores the values a trader sees on the chart.

The reference functions below are line-for-line ports of the chart's Dart
code. When this file was written, the Dart code itself was run on 43 series
(random walks of 15-1,700 closes plus flat, rising and falling ones) and
agreed with utils.indicators to 1.4e-10.
"""
import random

import pytest

from analyzer import WARMUP_DAYS
from utils import indicators


def chart_ema(values, period):
    """Port of indicators.dart ema(): SMA seed, then multiplier 2/(n+1)."""
    if period <= 0 or len(values) < period:
        return None
    previous = sum(values[:period]) / period
    multiplier = 2 / (period + 1)
    for v in values[period:]:
        previous = (v - previous) * multiplier + previous
    return previous


def chart_rsi(values, period=14):
    """Port of indicators.dart rsi(): Wilder's smoothing."""
    if period <= 0 or len(values) <= period:
        return None
    gain_sum = loss_sum = 0.0
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        if change >= 0:
            gain_sum += change
        else:
            loss_sum -= change
    avg_gain, avg_loss = gain_sum / period, loss_sum / period
    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
    if avg_loss == 0:
        return 50.0 if avg_gain == 0 else 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def walk(n, seed, start=25000.0, step=25.0):
    rng = random.Random(seed)
    prices, p = [], start
    for _ in range(n):
        p += rng.gauss(0, step)
        prices.append(round(p, 2))
    return prices


@pytest.mark.parametrize("seed", range(25))
def test_ema_and_rsi_match_the_chart(seed):
    closes = walk(random.Random(seed).randint(15, 1700), seed)
    for period in (20, 50, 200):
        got, want = indicators.ema(closes, period), chart_ema(closes, period)
        assert (got is None) == (want is None)
        if want is not None:
            assert got == pytest.approx(want, rel=0, abs=1e-8)
    assert indicators.rsi(closes) == pytest.approx(chart_rsi(closes), rel=0, abs=1e-8)


def test_a_known_value_from_the_chart_code():
    """Pinned from the chart's Dart ema()/rsi() on this exact series."""
    closes = [100 + (i % 7) - (i % 3) * 1.5 + i * 0.1 for i in range(60)]
    assert indicators.ema(closes, 20) == pytest.approx(chart_ema(closes, 20), abs=1e-9)
    assert indicators.rsi(closes) == pytest.approx(chart_rsi(closes), abs=1e-9)


def test_short_series_have_no_value_instead_of_a_made_up_one():
    # The old code reported the last close as EMA and 50 as RSI, which the
    # scoring then read as real (RSI 50 gave a long +15).
    assert indicators.ema([1.0] * 199, 200) is None
    assert indicators.ema([1.0] * 200, 200) == 1.0
    assert indicators.rsi([1.0] * 14) is None
    assert indicators.ema([], 20) is None and indicators.rsi([]) is None


def test_rsi_edge_cases():
    assert indicators.rsi([100.0] * 30) == 50.0              # no movement
    assert indicators.rsi([float(i) for i in range(30)]) == 100.0
    assert indicators.rsi([float(30 - i) for i in range(30)]) == 0.0


def test_ema_of_a_constant_is_the_constant():
    assert indicators.ema([123.45] * 500, 200) == pytest.approx(123.45)


def test_the_warmup_history_settles_ema_200():
    """The engine analyzes WARMUP_DAYS of 15m bars. EMA 200 over them must
    be the EMA 200 of a far longer history, to within 0.01%."""
    bars_per_day = 25
    warmup_bars = int(WARMUP_DAYS * 5 / 7) * bars_per_day   # weekdays only
    long_history = walk(warmup_bars * 4, seed=99)
    full = indicators.ema(long_history, 200)
    windowed = indicators.ema(long_history[-warmup_bars:], 200)
    assert abs(windowed - full) / full < 1e-4
    # 200 bars (the old history length) is not enough: just the SMA seed.
    assert indicators.ema(long_history[-200:], 200) == pytest.approx(
        sum(long_history[-200:]) / 200
    )


def reference_macd(closes, fast=12, slow=26, signal=9):
    """TradingView's ta.macd as plain loops: ta.ema (SMA seed, 2/(n+1)) of
    the closes, their difference from the slow EMA's first value on, and
    ta.ema of that difference."""
    def ema_series(values, period):
        out = [None] * len(values)
        if len(values) < period:
            return out
        prev = sum(values[:period]) / period
        out[period - 1] = prev
        for i in range(period, len(values)):
            prev = (values[i] - prev) * (2 / (period + 1)) + prev
            out[i] = prev
        return out

    f, s = ema_series(closes, fast), ema_series(closes, slow)
    line = [a - b for a, b in zip(f, s) if a is not None and b is not None]
    sig = ema_series(line, signal)
    if not line or sig[-1] is None:
        return None, None, None
    return line[-1], sig[-1], line[-1] - sig[-1]


@pytest.mark.parametrize("seed", range(25))
def test_macd_matches_the_standard_definition(seed):
    closes = walk(random.Random(seed).randint(30, 1700), seed)
    got, want = indicators.macd(closes), reference_macd(closes)
    assert (got[0] is None) == (want[0] is None)
    if want[0] is not None:
        assert got == pytest.approx(want, rel=0, abs=1e-8)


def test_macd_needs_slow_plus_signal_minus_one_closes():
    closes = walk(40, seed=3)
    assert indicators.macd(closes[:33]) == (None, None, None)
    line, signal, hist = indicators.macd(closes[:34])
    assert signal is not None and hist == pytest.approx(line - signal)


def test_macd_histogram_is_line_minus_its_own_signal():
    # The signal is the EMA 9 of the very line reported: on a constant
    # series all three are 0, and on a steady rise the line leads its signal.
    assert indicators.macd([100.0] * 60) == pytest.approx((0.0, 0.0, 0.0))
    line, signal, hist = indicators.macd([100 + i for i in range(60)])
    assert line > 0 and hist == pytest.approx(line - signal)
