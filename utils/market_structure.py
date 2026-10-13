"""
Market structure: swing labels (HH/HL/LH/LL), BOS / CHoCH and S/R zones.

The bot is the only place structure is *detected*. The chart draws what this
module returns and, for the candle that is still forming, only compares the
live price against the levels in ``watch`` (a faded "BOS?" / "CHoCH?" preview).
It never creates labels on its own, so the two can never disagree.

Rules
-----
Swing points
    A swing high is a candle whose high is strictly above the highs of the
    ``strength`` candles before it and at least as high as the ``strength``
    candles after it (lows mirrored). It is therefore *confirmed* only once
    ``strength`` more candles have closed - every label is late by design.
    Swings alternate high/low. When two pivots of the same kind follow each
    other, the extreme candle between them is inserted as the opposite swing
    (``derived``), so earlier labels never have to be moved or deleted.

Labels
    Highs are compared only with the previous swing high (HH / LH / EQH),
    lows only with the previous swing low (HL / LL / EQL).

Sequence
    The labels of the latest swing high and swing low:
    HH + HL -> "up", LH + LL -> "down", anything else -> "mixed"
    ("none" until both a high and a low have a label).

Breaks (closing prices only, wicks never count)
    A candle that closes beyond the latest unbroken swing level breaks it:
    ===============  ======================  ==========================
    trend            close above last high   close below last low
    ===============  ======================  ==========================
    up               BOS up                  CHoCH down -> trend down
    down             CHoCH up -> trend up    BOS down
    none             (level broken, no event: no structure to trade)
    ===============  ======================  ==========================

Trend
    * none -> up/down once the sequence is cleanly up/down.
    * After a CHoCH the new trend is *unconfirmed* until the next swing on the
      other side, formed after the break, agrees (an LH after a CHoCH down, an
      HL after a CHoCH up). If that swing disagrees instead, the CHoCH failed
      and the trend is "none".
    * A confirmed trend whose sequence turns mixed (e.g. HH then LL, or LH then
      HL) becomes "none": the market is ranging, structure is not traded.

Zones
    Every swing leaves a zone from its candle's body edge to its wick extreme
    (resistance above swing highs, support below swing lows). A zone is removed
    once a candle closes beyond its far edge.

Only closed candles may be passed in (see ``closed_candles``). The result for
a series is always a prefix-extension of the result for any earlier part of
that series: confirmed swings and breaks never change afterwards.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

DEFAULT_STRENGTH = 3
MAX_ZONES_PER_SIDE = 4

#: 15:30 IST - the NSE session close - in UTC minutes. A bar that would run
#: past it (e.g. the 15:15 1h bar) is complete at the close.
SESSION_CLOSE_UTC = (10, 0)


@dataclass(frozen=True)
class Bar:
    time: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass
class Swing:
    kind: str  # "high" | "low"
    index: int
    time: datetime
    price: float
    label: Optional[str]  # HH/LH/EQH, HL/LL/EQL; None for the first of its kind
    confirmed_index: int
    confirmed_time: datetime
    derived: bool = False
    broken_index: Optional[int] = None
    broken_time: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "time": _iso(self.time),
            "price": self.price,
            "label": self.label,
            "confirmed_time": _iso(self.confirmed_time),
            "derived": self.derived,
            "broken_time": _iso(self.broken_time),
        }


@dataclass(frozen=True)
class StructureBreak:
    type: str  # "BOS" | "CHoCH"
    direction: str  # "up" | "down"
    price: float  # the swing level that was broken
    swing_time: datetime
    swing_label: Optional[str]
    index: int  # the candle that closed beyond the level
    time: datetime
    close: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "direction": self.direction,
            "price": self.price,
            "swing_time": _iso(self.swing_time),
            "swing_label": self.swing_label,
            "time": _iso(self.time),
            "close": self.close,
        }


@dataclass
class Zone:
    kind: str  # "resistance" | "support"
    top: float
    bottom: float
    time: datetime
    swing_label: Optional[str]
    broken_time: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "top": self.top,
            "bottom": self.bottom,
            "time": _iso(self.time),
            "swing_label": self.swing_label,
        }


@dataclass
class MarketStructure:
    strength: int
    swings: List[Swing]
    breaks: List[StructureBreak]
    zones: List[Zone]  # active (unbroken) zones only, newest last
    trend: str  # "up" | "down" | "none"
    trend_confirmed: bool
    sequence: str  # "up" | "down" | "mixed" | "none"
    last_high: Optional[Swing]  # latest swing high, if still unbroken
    last_low: Optional[Swing]
    last_closed_time: Optional[datetime]
    candles_used: int

    def watch(self) -> Dict[str, Optional[Dict[str, Any]]]:
        """Levels the chart may preview against the live price.

        ``event`` is what a *close* beyond the level would be. It is None when
        there is no trend - a break there means nothing, so nothing is shown.
        """
        def level(swing: Optional[Swing], direction: str) -> Optional[Dict[str, Any]]:
            if swing is None:
                return None
            if self.trend == "none":
                event = None
            elif self.trend == direction:
                event = "BOS"
            else:
                event = "CHoCH"
            return {
                "price": swing.price,
                "time": _iso(swing.time),
                "label": swing.label,
                "event": event,
            }

        return {"up": level(self.last_high, "up"), "down": level(self.last_low, "down")}

    def summary(self) -> Dict[str, Any]:
        """Compact state for the decision engine and status endpoints."""
        last_break = self.breaks[-1] if self.breaks else None
        return {
            "trend": self.trend,
            "trend_confirmed": self.trend_confirmed,
            "sequence": self.sequence,
            "tradeable": self.trend != "none" and self.trend_confirmed,
            "last_break": last_break.to_dict() if last_break else None,
            "last_high": self.last_high.price if self.last_high else None,
            "last_low": self.last_low.price if self.last_low else None,
        }

    def to_dict(self, since: Optional[datetime] = None) -> Dict[str, Any]:
        """``since`` limits swings and breaks to those a chart starting there
        can show (a break counts when its closing candle is in view)."""
        swings = self.swings if since is None else [s for s in self.swings if s.time >= since]
        breaks = self.breaks if since is None else [b for b in self.breaks if b.time >= since]
        return {
            "strength": self.strength,
            "trend": self.trend,
            "trend_confirmed": self.trend_confirmed,
            "sequence": self.sequence,
            "last_closed_time": _iso(self.last_closed_time),
            "candles_used": self.candles_used,
            "swings": [s.to_dict() for s in swings],
            "breaks": [b.to_dict() for b in breaks],
            "zones": [z.to_dict() for z in self.zones],
            "watch": self.watch(),
        }


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _field(candle: Any, name: str) -> Any:
    return candle[name] if isinstance(candle, dict) else getattr(candle, name)


def to_bars(candles: Iterable[Any]) -> List[Bar]:
    """Accepts Candle models or candle dicts (``timestamp`` + OHLC)."""
    return [
        Bar(
            time=_field(c, "timestamp"),
            open=float(_field(c, "open")),
            high=float(_field(c, "high")),
            low=float(_field(c, "low")),
            close=float(_field(c, "close")),
        )
        for c in candles
    ]


def bar_end(start: datetime, minutes: int) -> datetime:
    """When the bar opened at ``start`` is complete (capped at the session close)."""
    end = start + timedelta(minutes=minutes)
    close = start.replace(
        hour=SESSION_CLOSE_UTC[0], minute=SESSION_CLOSE_UTC[1], second=0, microsecond=0
    )
    if start < close < end:
        return close
    return end


def closed_candles(candles: List[Any], minutes: int, now: datetime) -> List[Any]:
    """Drop the bar still forming at ``now`` (naive UTC, like candle times).

    Structure is only ever computed from closed candles: a forming candle's
    high/low/close still change, and labels built on it would change too.
    """
    result = list(candles)
    while result and bar_end(_field(result[-1], "timestamp"), minutes) > now:
        result.pop()
    return result


class _Detector:
    def __init__(self, bars: List[Bar], strength: int):
        self.bars = bars
        self.n = strength
        self.swings: List[Swing] = []
        self.breaks: List[StructureBreak] = []
        self.zones: List[Zone] = []  # active (unbroken) zones only
        self.trend = "none"
        self.confirmed = False
        # Candle index of the CHoCH awaiting confirmation: only a swing formed
        # *after* the break can confirm it.
        self.choch_index = -1
        self.last_high: Optional[Swing] = None
        self.last_low: Optional[Swing] = None
        self.prev_high: Optional[Swing] = None
        self.prev_low: Optional[Swing] = None

    # ── pivots ──────────────────────────────────────────────────
    def _pivot_high(self, p: int) -> bool:
        high = self.bars[p].high
        n = self.n
        return all(high > b.high for b in self.bars[p - n:p]) and all(
            high >= b.high for b in self.bars[p + 1:p + n + 1]
        )

    def _pivot_low(self, p: int) -> bool:
        low = self.bars[p].low
        n = self.n
        return all(low < b.low for b in self.bars[p - n:p]) and all(
            low <= b.low for b in self.bars[p + 1:p + n + 1]
        )

    # ── swings ──────────────────────────────────────────────────
    def _add_pivot(self, kind: str, p: int, k: int) -> None:
        last = self.swings[-1] if self.swings else None
        if last is not None and last.kind == kind:
            # Two highs (or lows) in a row: the extreme between them is the
            # missing opposite swing. Pivots of one kind are > n bars apart,
            # so there is always at least one bar in between.
            between = range(last.index + 1, p)
            if kind == "high":
                j = min(between, key=lambda i: (self.bars[i].low, i))
                self._add_swing("low", j, k, derived=True)
            else:
                j = min(between, key=lambda i: (-self.bars[i].high, i))
                self._add_swing("high", j, k, derived=True)
        self._add_swing(kind, p, k, derived=False)

    def _add_swing(self, kind: str, index: int, k: int, derived: bool) -> None:
        bar = self.bars[index]
        if kind == "high":
            price = bar.high
            prev = self.prev_high
            if prev is None:
                label = None
            elif price > prev.price:
                label = "HH"
            elif price < prev.price:
                label = "LH"
            else:
                label = "EQH"
        else:
            price = bar.low
            prev = self.prev_low
            if prev is None:
                label = None
            elif price > prev.price:
                label = "HL"
            elif price < prev.price:
                label = "LL"
            else:
                label = "EQL"

        swing = Swing(
            kind=kind,
            index=index,
            time=bar.time,
            price=price,
            label=label,
            confirmed_index=k,
            confirmed_time=self.bars[k].time,
            derived=derived,
        )
        self.swings.append(swing)
        if kind == "high":
            self.prev_high = swing
            self.last_high = swing  # replaces any older unbroken high
            self.zones.append(Zone("resistance", price, max(bar.open, bar.close), bar.time, label))
        else:
            self.prev_low = swing
            self.last_low = swing
            self.zones.append(Zone("support", min(bar.open, bar.close), price, bar.time, label))
        self._update_trend_on_swing(swing)

    def sequence(self) -> str:
        high = self.prev_high.label if self.prev_high else None
        low = self.prev_low.label if self.prev_low else None
        if high is None or low is None:
            return "none"
        if high == "HH" and low == "HL":
            return "up"
        if high == "LH" and low == "LL":
            return "down"
        return "mixed"

    def _update_trend_on_swing(self, swing: Swing) -> None:
        if self.trend != "none" and not self.confirmed:
            # Waiting for the swing that confirms the CHoCH. A swing that
            # formed before the break (and was only confirmed after it) is
            # part of the old structure and cannot confirm the new trend.
            if swing.index <= self.choch_index:
                return
            if self.trend == "down" and swing.kind == "high":
                if swing.label == "LH":
                    self.confirmed = True
                else:
                    self.trend = "none"
            elif self.trend == "up" and swing.kind == "low":
                if swing.label == "HL":
                    self.confirmed = True
                else:
                    self.trend = "none"
            return

        seq = self.sequence()
        if self.trend == "none":
            if seq in ("up", "down"):
                self.trend, self.confirmed = seq, True
        elif seq != self.trend:
            # e.g. an LH or LL inside an uptrend: no longer HH/HL.
            self.trend, self.confirmed = "none", False

    # ── breaks ──────────────────────────────────────────────────
    def _check_breaks(self, k: int) -> None:
        bar = self.bars[k]
        close = bar.close

        high = self.last_high
        if high is not None and high.broken_index is None and close > high.price:
            high.broken_index, high.broken_time = k, bar.time
            if self.trend == "up":
                self._record("BOS", "up", high, k)
            elif self.trend == "down":
                self._record("CHoCH", "up", high, k)
                self.trend, self.confirmed, self.choch_index = "up", False, k

        low = self.last_low
        if low is not None and low.broken_index is None and close < low.price:
            low.broken_index, low.broken_time = k, bar.time
            if self.trend == "down":
                self._record("BOS", "down", low, k)
            elif self.trend == "up":
                self._record("CHoCH", "down", low, k)
                self.trend, self.confirmed, self.choch_index = "down", False, k

        still_active = []
        for zone in self.zones:
            if (zone.kind == "resistance" and close > zone.top) or (
                zone.kind == "support" and close < zone.bottom
            ):
                zone.broken_time = bar.time
            else:
                still_active.append(zone)
        self.zones = still_active

    def _record(self, kind: str, direction: str, swing: Swing, k: int) -> None:
        bar = self.bars[k]
        self.breaks.append(
            StructureBreak(
                type=kind,
                direction=direction,
                price=swing.price,
                swing_time=swing.time,
                swing_label=swing.label,
                index=k,
                time=bar.time,
                close=bar.close,
            )
        )

    # ── driver ──────────────────────────────────────────────────
    def run(self) -> MarketStructure:
        for k in range(len(self.bars)):
            self.step(k)
        return self.result()

    def append(self, bar: Bar) -> None:
        """Feed the next closed bar (incremental use)."""
        self.bars.append(bar)
        self.step(len(self.bars) - 1)

    def step(self, k: int) -> None:
        """Process candle ``k`` closing - all state depends only on bars <= k."""
        n = self.n
        # Breaks first, against the structure as it stood before candle k
        # closed - exactly the levels the chart was previewing (``watch``)
        # while k was forming, so a preview and its confirmation agree.
        self._check_breaks(k)
        # Candle k closing confirms (or rejects) the pivot n bars back.
        p = k - n
        if p >= n:
            is_high = self._pivot_high(p)
            is_low = self._pivot_low(p)
            if is_high and is_low:
                last = self.swings[-1] if self.swings else None
                if last is not None:
                    order = ("low", "high") if last.kind == "high" else ("high", "low")
                else:
                    # An outside bar: a green one made its low first.
                    bar = self.bars[p]
                    order = ("low", "high") if bar.close >= bar.open else ("high", "low")
                for kind in order:
                    self._add_pivot(kind, p, k)
            elif is_high:
                self._add_pivot("high", p, k)
            elif is_low:
                self._add_pivot("low", p, k)

    def result(self) -> MarketStructure:
        n = self.n
        active = self.zones
        resistance = [z for z in active if z.kind == "resistance"][-MAX_ZONES_PER_SIDE:]
        support = [z for z in active if z.kind == "support"][-MAX_ZONES_PER_SIDE:]
        zones = sorted(resistance + support, key=lambda z: z.time)

        def unbroken(swing: Optional[Swing]) -> Optional[Swing]:
            return swing if swing is not None and swing.broken_index is None else None

        return MarketStructure(
            strength=n,
            swings=list(self.swings),
            breaks=list(self.breaks),
            zones=zones,
            trend=self.trend,
            trend_confirmed=self.trend != "none" and self.confirmed,
            sequence=self.sequence(),
            last_high=unbroken(self.last_high),
            last_low=unbroken(self.last_low),
            last_closed_time=self.bars[-1].time if self.bars else None,
            candles_used=len(self.bars),
        )


class StructureTracker:
    """Market structure of one series from a fixed start, fed incrementally.

    Feeding closed bars one batch at a time gives exactly the same result as
    analysing the whole series at once (the detector is causal), without
    re-reading months of history on every request.
    """

    def __init__(self, strength: int = DEFAULT_STRENGTH):
        if strength < 1:
            raise ValueError("strength must be at least 1")
        self._detector = _Detector([], strength)

    @property
    def last_time(self) -> Optional[datetime]:
        bars = self._detector.bars
        return bars[-1].time if bars else None

    def feed(self, candles: Iterable[Any]) -> int:
        """Add closed candles newer than the last one fed; returns how many."""
        added = 0
        for bar in sorted(to_bars(candles), key=lambda b: b.time):
            last = self.last_time
            if last is not None and bar.time <= last:
                continue
            self._detector.append(bar)
            added += 1
        return added

    def result(self) -> MarketStructure:
        return self._detector.result()


def analyze_structure(candles: Iterable[Any], strength: int = DEFAULT_STRENGTH) -> MarketStructure:
    """Market structure of *closed* candles, oldest first."""
    if strength < 1:
        raise ValueError("strength must be at least 1")
    return _Detector(to_bars(candles), strength).run()
