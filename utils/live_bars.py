"""
Live forming candles for every subscribed symbol, built from its ticks.

Only the engine's active symbol has tick-built candle buffers (the
Analyzer's 5m/15m/1h). Every other symbol - and the active one on 1m/30m -
is served from broker history cached for ``HISTORY_CACHE_TTL``, so its
forming candle used to freeze between cache refreshes even though the data
socket delivers ticks for the whole watchlist.

Broker history stays authoritative: it decides the bar's open and everything
up to the moment it was fetched. Ticks only extend the newest bar:

- high/low: max/min with every tick inside the bar. Idempotent, so ticks the
  history already covered change nothing.
- close: the newest tick inside the bar, if it arrived after the fetch.
- volume: growth of the day's cumulative traded volume since the fetch.

When the next bar has started but the history does not have it yet, it is
built from ticks - only if this process was already receiving the symbol
before the bar opened, so its first tick is the bar's real open.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from models.candle import Candle

#: Ticks are kept this long: more than the widest live bar (1h).
KEEP = timedelta(hours=2)

# NSE cash session in UTC (09:15-15:30 IST).
SESSION_OPEN_MIN = 3 * 60 + 45
SESSION_CLOSE_MIN = 10 * 60


def in_session(at: datetime) -> bool:
    """True during the NSE cash session (Mon-Fri 09:15-15:30 IST), naive UTC."""
    if at.weekday() >= 5:
        return False
    minutes = at.hour * 60 + at.minute
    return SESSION_OPEN_MIN <= minutes < SESSION_CLOSE_MIN


def session_close(at: datetime) -> datetime:
    return at.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        minutes=SESSION_CLOSE_MIN
    )


@dataclass
class _Minute:
    """Ticks of one symbol within one exchange minute."""

    open: float
    high: float
    low: float
    close: float
    #: Receive time of the newest tick (comparable with a history fetch time).
    received_at: datetime
    #: Day's cumulative traded volume at the newest tick.
    day_volume: int
    #: Day's cumulative traded volume before this minute's first tick.
    day_volume_before: Optional[int]


class LiveBarTracker:
    """Per-symbol one-minute tick aggregates, applied to history on request."""

    def __init__(self):
        self._minutes: Dict[str, Dict[datetime, _Minute]] = {}
        self._day_volume: Dict[str, int] = {}
        #: Receive time of the first tick since the feed was last known whole.
        self._since: Dict[str, datetime] = {}

    def reset_continuity(self) -> None:
        """The feed had a gap (socket rebuilt): ticks may have been missed,
        so no bar may be opened from ticks until a full bar has been seen."""
        self._since.clear()

    def volume_mark(self, symbol: str) -> Optional[int]:
        """Day's cumulative volume as of now - taken just before a history fetch."""
        return self._day_volume.get(symbol)

    def record(
        self,
        symbol: str,
        price: float,
        day_volume: int,
        received_at: datetime,
        exchange_at: Optional[datetime] = None,
    ) -> None:
        """One tick. ``exchange_at`` (trade time) places it in its bar;
        without one, the receive time is used."""
        if not symbol or not price or price <= 0:
            return
        at = exchange_at or received_at
        if not in_session(at):
            return
        minute = at.replace(second=0, microsecond=0)
        minutes = self._minutes.setdefault(symbol, {})
        self._since.setdefault(symbol, received_at)
        day_volume = int(day_volume or 0)
        bar = minutes.get(minute)
        if bar is None:
            minutes[minute] = _Minute(
                open=price,
                high=price,
                low=price,
                close=price,
                received_at=received_at,
                day_volume=day_volume,
                day_volume_before=self._day_volume.get(symbol),
            )
            cutoff = minute - KEEP
            for old in [t for t in minutes if t < cutoff]:
                del minutes[old]
        else:
            bar.high = max(bar.high, price)
            bar.low = min(bar.low, price)
            if received_at >= bar.received_at:
                bar.close = price
                bar.received_at = received_at
                bar.day_volume = day_volume
        self._day_volume[symbol] = day_volume

    def _inside(self, symbol: str, start: datetime, end: datetime) -> List[tuple]:
        minutes = self._minutes.get(symbol) or {}
        return sorted((t, m) for t, m in minutes.items() if start <= t < end)

    def apply(
        self,
        symbol: str,
        candles: List[Candle],
        step: timedelta,
        fetched_at: Optional[datetime],
        volume_mark: Optional[int],
        now: datetime,
    ) -> List[Candle]:
        """``candles`` (ascending history fetched at ``fetched_at``) with the
        newest bar extended by later ticks and, if history does not have it
        yet, the bar forming now appended. Never mutates ``candles``."""
        if not candles or fetched_at is None or not self._minutes.get(symbol):
            return candles

        result = list(candles)
        last = result[-1]
        end = min(last.timestamp + step, session_close(last.timestamp))
        inside = self._inside(symbol, last.timestamp, end)
        if inside:
            newest = max((m for _, m in inside), key=lambda m: m.received_at)
            high = max(last.high, max(m.high for _, m in inside))
            low = min(last.low, min(m.low for _, m in inside))
            close, volume = last.close, last.volume
            if newest.received_at > fetched_at:
                close = newest.close
                if volume_mark is not None and newest.day_volume > volume_mark:
                    volume = last.volume + (newest.day_volume - volume_mark)
            if (high, low, close, volume) != (last.high, last.low, last.close, last.volume):
                result[-1] = last.model_copy(
                    update={"high": high, "low": low, "close": close, "volume": volume}
                )

        forming = self._forming_bar(symbol, last, step, now)
        if forming is not None:
            result.append(forming)
        return result

    def _forming_bar(
        self, symbol: str, last: Candle, step: timedelta, now: datetime
    ) -> Optional[Candle]:
        """The bar after ``last`` that ``now`` falls in, built from ticks -
        only within the same session, and only when its open was observed."""
        start = last.timestamp + step
        if start > now or start >= session_close(last.timestamp):
            return None
        if now >= start + step:
            # More than one bar is missing: not a gap ticks may paper over.
            return None
        since = self._since.get(symbol)
        if since is None or since >= start:
            return None
        inside = self._inside(symbol, start, min(start + step, session_close(start)))
        if not inside:
            return None
        first = inside[0][1]
        newest = max((m for _, m in inside), key=lambda m: m.received_at)
        volume = 0
        if first.day_volume_before is not None:
            volume = max(0, newest.day_volume - first.day_volume_before)
        return Candle(
            timestamp=start,
            open=first.open,
            high=max(m.high for _, m in inside),
            low=min(m.low for _, m in inside),
            close=newest.close,
            volume=volume,
            timeframe=last.timeframe,
            symbol=last.symbol,
            symbol_name=last.symbol_name,
        )
