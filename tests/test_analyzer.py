"""
Regression tests for Analyzer.add_tick / _finalize_candle.

Three bugs fixed together:
  1. A bar's own close could never be observed from a tick inside that bar
     (a tick's timestamp is always floored into the bar that contains it,
     so `_is_candle_closed_at` compared a candle against itself and could
     never be true). This meant candles were NEVER finalized and the
     trading decision cycle NEVER ran, no matter how long the bot ran.
     The fix detects a seal on bar rollover instead: the first tick of a
     new bar hands back the just-sealed previous bar.
  2. _finalize_candle always read ``self.candles_15m`` no matter which
     timeframe's candle it was finalizing, so a closing 5m/1h candle got
     indicator values computed from the *15m* series.
  3. add_tick returned whichever of 5m/15m/1h happened to close last on a
     given tick, so a 5m or 1h close could trigger the full trading decision
     cycle (bot._on_candle_close) using off-timeframe indicator data.
"""
from datetime import datetime, timedelta

from analyzer import Analyzer


def _feed_ticks(analyzer: Analyzer, start: datetime, prices, minutes_per_tick: int):
    """Feed one tick per `minutes_per_tick`, returning the list of add_tick results."""
    results = []
    ts = start
    for price in prices:
        results.append(analyzer.add_tick(price, volume=100, timestamp=ts))
        ts += timedelta(minutes=minutes_per_tick)
    return results


def test_bar_seals_on_next_bars_first_tick():
    """
    The defining regression: before the fix, no candle was ever finalized
    (closed_candle was always None), so the bot never ran a decision cycle.
    """
    analyzer = Analyzer()
    start = datetime(2026, 1, 5, 9, 15)  # Monday, on a 15m/1h boundary

    # 16 one-minute ticks: the 17th tick (minute 16) is the first tick of
    # the *next* 15m bar (9:30), which is what seals the 9:15 bar.
    prices = [100 + i for i in range(17)]
    results = _feed_ticks(analyzer, start, prices, minutes_per_tick=1)

    closed = [r for r in results if r is not None]
    assert len(closed) == 1, "the 15m bar must seal exactly once, on rollover"
    assert closed[0].timeframe == "15m"
    assert closed[0].timestamp == start


def test_only_15m_close_is_returned_as_closed_candle():
    """A 5m-only rollover (no 15m/1h boundary crossed) must not surface a closed candle."""
    analyzer = Analyzer()
    start = datetime(2026, 1, 5, 9, 15)

    # 6 one-minute ticks: the 6th tick rolls the 5m bar (9:15 -> 9:20) but
    # crosses no 15m or 1h boundary.
    prices = [100, 101, 102, 103, 104, 105]
    results = _feed_ticks(analyzer, start, prices, minutes_per_tick=1)

    assert all(r is None for r in results), (
        "a bare 5m rollover must not trigger the decision cycle"
    )
    assert len(analyzer.candles_5m) == 2  # 9:15 bar (sealed) + 9:20 bar (open)
    # The first (now-sealed) 5m candle should have been finalized.
    assert analyzer.candles_5m[0].rsi is not None
    # The still-open second bar must not have indicators yet.
    assert analyzer.candles_5m[1].rsi is None


def test_indicators_computed_from_own_timeframe_series():
    """
    5m and 15m candles built from the same tick stream must get indicators
    derived from their *own* series, not from candles_15m.
    """
    analyzer = Analyzer()
    start = datetime(2026, 1, 5, 9, 15)

    # 61 one-minute ticks: enough rollovers to seal several 5m and 15m bars.
    prices = [100 + i * 0.5 for i in range(61)]
    _feed_ticks(analyzer, start, prices, minutes_per_tick=1)

    sealed_5m = [c for c in analyzer.candles_5m if c.rsi is not None]
    sealed_15m = [c for c in analyzer.candles_15m if c.rsi is not None]
    assert len(sealed_5m) >= 2
    assert len(sealed_15m) >= 2
    # 5m bars seal 3x more often than 15m bars over the same tick stream.
    assert len(sealed_5m) != len(sealed_15m)

    # Regression guard for the original series bug: before the fix, every
    # finalized candle (5m or 15m) computed ATR/VWAP from the *same*
    # self.candles_15m series, so a 5m candle's atr/vwap would equal the 15m
    # series' values at that point rather than being derived from its own
    # (differently-sized) set of 5m bars.
    last_5m = sealed_5m[-1]
    last_15m = sealed_15m[-1]
    assert last_5m.atr != last_15m.atr
    assert last_5m.vwap != last_15m.vwap


def test_1h_bars_open_at_the_session_open_not_the_clock_hour():
    """Fyers 1h bars open at 09:15, 10:15 ... IST (03:45, 04:45 ... UTC);
    tick-built bars must use the same boundaries or they never line up
    with the history the buffers are seeded with."""
    analyzer = Analyzer()
    start = datetime(2026, 1, 5, 3, 45)  # 09:15 IST
    _feed_ticks(analyzer, start, [100 + i for i in range(121)], minutes_per_tick=1)

    assert [c.timestamp for c in analyzer.candles_1h] == [
        datetime(2026, 1, 5, 3, 45),
        datetime(2026, 1, 5, 4, 45),
        datetime(2026, 1, 5, 5, 45),
    ]
    # A full hour per bar: the 09:15 bar is not cut at 10:00 IST.
    first = analyzer.candles_1h[0]
    assert (first.open, first.close) == (100, 159)


def test_1h_tick_continues_a_session_aligned_history_bar():
    from models.candle import Candle

    analyzer = Analyzer()
    seeded = Candle(timestamp=datetime(2026, 1, 5, 4, 45), open=100, high=101,
                    low=99, close=100, volume=500, timeframe="1h")
    analyzer.candles_1h = [seeded]
    # 10:45 IST: inside the 10:15 bar, past the old (wrong) 10:00 boundary.
    analyzer.add_tick(105, 7, datetime(2026, 1, 5, 5, 15))

    assert len(analyzer.candles_1h) == 1
    assert analyzer.candles_1h[0].high == 105
    assert analyzer.candles_1h[0].volume == 507


def test_5m_and_15m_boundaries_are_unchanged():
    from utils.helpers import session_bar_start

    ts = datetime(2026, 1, 5, 6, 59, 30)
    assert session_bar_start(ts, 5) == datetime(2026, 1, 5, 6, 55)
    assert session_bar_start(ts, 15) == datetime(2026, 1, 5, 6, 45)
    assert session_bar_start(ts, 1) == datetime(2026, 1, 5, 6, 59)
    assert session_bar_start(ts, 30) == datetime(2026, 1, 5, 6, 45)
    assert session_bar_start(ts, 60) == datetime(2026, 1, 5, 6, 45)
    assert session_bar_start(datetime(2026, 1, 5, 6, 44), 60) == datetime(2026, 1, 5, 5, 45)
