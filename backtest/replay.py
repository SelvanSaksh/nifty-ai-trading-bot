"""
Feature pass: replay 1-minute history as ticks through the live Analyzer.

Each 1m candle becomes four ticks (open, then low/high in the order the bar
most likely moved, then close), fed to ``Analyzer.add_tick`` exactly as the
engine feeds live ticks. Every 15m close produces the same analysis the live
decision cycle sees. This expensive pass runs once; trading variants are
then simulated cheaply over its output (see simulator.py).
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List

from analyzer import Analyzer
from models.candle import Candle
from utils.live_bars import in_session

#: Neutral market context. The decision engine does not score it, and the
#: live values (options chain, news) have no history to replay.
OFFLINE_CONTEXT: Dict[str, Any] = {
    "options_available": False, "pcr": None, "vix_regime": None, "max_pain": None,
    "call_oi": None, "put_oi": None, "oi_change": {"call": None, "put": None},
    "news_blackout": False, "active_blackouts": [], "nearest_expiry": None, "bias": None,
}


@dataclass(frozen=True)
class Tick:
    time: datetime   # naive UTC
    price: float


@dataclass
class DecisionPoint:
    tick_index: int              # the tick that closed the candle (live: first tick of the next bar)
    candle: Candle               # the closed 15m candle
    analysis: Dict[str, Any]     # what Analyzer.analyze returned


@dataclass
class Replay:
    ticks: List[Tick]
    decisions: List[DecisionPoint]


class OfflineAnalyzer(Analyzer):
    """The live Analyzer with no network calls and bounded buffers.

    A running engine holds the ~200 history bars it starts with plus what it
    builds since; buffers are capped at ``max_bars`` so a year-long replay
    stays fast and sees series of the same length the engine sees.
    """

    def __init__(self, max_bars: int = 400):
        super().__init__()
        self.max_bars = max_bars

    async def _get_market_context(self) -> Dict[str, Any]:
        return dict(OFFLINE_CONTEXT)

    def add_tick(self, price, volume, timestamp):
        closed = super().add_tick(price, volume, timestamp)
        for series in (self.candles_5m, self.candles_15m, self.candles_1h):
            if len(series) > self.max_bars:
                del series[: len(series) - self.max_bars]
        return closed


def minute_ticks(candle: Candle) -> List[Tick]:
    """Open, the nearer extreme, the farther extreme, close."""
    t = candle.timestamp
    if candle.close >= candle.open:
        path = (candle.open, candle.low, candle.high, candle.close)
    else:
        path = (candle.open, candle.high, candle.low, candle.close)
    return [Tick(t + timedelta(seconds=15 * i), p) for i, p in enumerate(path)]


async def replay(minutes: List[Candle], max_bars: int = 400) -> Replay:
    """Run 1m history through the Analyzer; collect ticks and 15m decisions."""
    analyzer = OfflineAnalyzer(max_bars)
    ticks: List[Tick] = []
    decisions: List[DecisionPoint] = []
    for candle in minutes:
        if not in_session(candle.timestamp):
            continue
        path = minute_ticks(candle)
        for n, tick in enumerate(path):
            # The bar's volume arrives with its last tick (bar totals match).
            volume = int(candle.volume) if n == len(path) - 1 else 0
            closed = analyzer.add_tick(tick.price, volume, tick.time)
            ticks.append(tick)
            if closed is not None:
                analysis = await analyzer.analyze(closed)
                decisions.append(DecisionPoint(len(ticks) - 1, closed, analysis))
    return Replay(ticks, decisions)
