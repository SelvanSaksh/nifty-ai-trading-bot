"""Strategy inputs: session VWAP and RSI divergence in both directions."""
from datetime import datetime, timedelta

import pytest

from analyzer import Analyzer
from models.candle import Candle
from utils import indicators


def bar(ts: datetime, high: float, low: float, close: float, volume: int, rsi=None) -> Candle:
    return Candle(timestamp=ts, open=close, high=high, low=low, close=close,
                  volume=volume, rsi=rsi)


# Naive UTC, as every candle in the app. 03:45 UTC = 09:15 IST.
MON_OPEN = datetime(2026, 10, 26, 3, 45)
TUE_OPEN = datetime(2026, 10, 27, 3, 45)


def test_vwap_uses_only_the_current_session():
    monday = [bar(MON_OPEN + timedelta(minutes=15 * i), 110, 90, 100, 1_000) for i in range(25)]
    tuesday = [
        bar(TUE_OPEN, 206, 200, 203, 100),                        # typical 203
        bar(TUE_OPEN + timedelta(minutes=15), 212, 206, 209, 300),  # typical 209
    ]
    expected = (203 * 100 + 209 * 300) / 400

    assert indicators.vwap(monday + tuesday) == pytest.approx(expected)
    # Monday's own VWAP is unaffected by Tuesday.
    assert indicators.vwap(monday) == pytest.approx(100)


def test_vwap_day_follows_ist_not_utc():
    # 19:00 UTC on the 26th is 00:30 IST on the 27th: same IST day as Tuesday's open.
    late = bar(datetime(2026, 10, 26, 19, 0), 300, 300, 300, 100)
    earlier_ist_day = bar(datetime(2026, 10, 26, 9, 0), 50, 50, 50, 100)
    tue = bar(TUE_OPEN, 100, 100, 100, 100)

    assert indicators.vwap([earlier_ist_day, late, tue]) == pytest.approx(200)


def test_vwap_without_session_volume_is_the_last_typical_price():
    monday = [bar(MON_OPEN, 110, 90, 100, 1_000)]
    tuesday = [bar(TUE_OPEN, 206, 200, 203, 0), bar(TUE_OPEN + timedelta(minutes=15), 212, 206, 209, 0)]
    assert indicators.vwap(monday + tuesday) == pytest.approx(209)


def test_vwap_empty():
    assert indicators.vwap([]) == 0.0


def series(tail):
    """20 flat bars, then ``tail`` of (high, low, rsi)."""
    start = datetime(2026, 10, 26, 3, 45)
    flat = [bar(start + timedelta(minutes=15 * i), 101, 99, 100, 1_000, rsi=50) for i in range(20)]
    tail_bars = [
        bar(start + timedelta(minutes=15 * (20 + i)), h, l, (h + l) / 2, 1_000, rsi=r)
        for i, (h, l, r) in enumerate(tail)
    ]
    return flat + tail_bars


def test_bearish_divergence_higher_highs_falling_rsi():
    candles = series([(110, 100, 70), (111, 101, 68), (112, 102, 66), (113, 103, 64), (114, 104, 62)])
    assert Analyzer()._detect_rsi_divergence(candles) == "bearish_divergence"


def test_bullish_divergence_still_detected():
    candles = series([(100, 90, 30), (99, 89, 32), (98, 88, 34), (97, 87, 36), (96, 86, 38)])
    assert Analyzer()._detect_rsi_divergence(candles) == "bullish_divergence"


@pytest.mark.parametrize("tail", [
    # Higher highs with rising RSI: momentum agrees, no divergence.
    [(110, 100, 60), (111, 101, 62), (112, 102, 64), (113, 103, 66), (114, 104, 68)],
    # Falling RSI but one high fails to rise.
    [(110, 100, 70), (111, 101, 68), (111, 101, 66), (113, 103, 64), (114, 104, 62)],
])
def test_no_divergence(tail):
    assert Analyzer()._detect_rsi_divergence(series(tail)) is None


def _analysis(candle, patterns=()):
    from models.enums import Direction
    return {"candle": candle, "patterns": list(patterns), "divergence": None,
            "fvg": None, "liquidity_sweep": None, "higher_timeframe_trend": Direction.WAIT}


def test_no_points_for_volume_on_either_side():
    from decision_engine import DecisionEngine
    engine = DecisionEngine()
    # Strong uptrend 25 + above VWAP 10 + RSI 55 -> 15 + MACD 10 = 60, nothing for volume.
    up = Candle(timestamp=TUE_OPEN, open=100, high=101, low=99, close=100, volume=5_000_000,
                ema_20=99, ema_50=98, ema_200=97, vwap=95, rsi=55, macd_hist=1.0)
    assert engine._score_long(_analysis(up)) == 60
    # The mirror: strong downtrend 25 + below VWAP 10 + RSI 45 -> 15 + MACD 10 = 60.
    down = Candle(timestamp=TUE_OPEN, open=100, high=101, low=99, close=100, volume=5_000_000,
                  ema_20=101, ema_50=102, ema_200=103, vwap=105, rsi=45, macd_hist=-1.0)
    assert engine._score_short(_analysis(down)) == 60
