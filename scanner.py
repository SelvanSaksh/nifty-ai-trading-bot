"""
Watchlist scanner: what the decision engine makes of every watchlist symbol's
latest closed 15m candle, ranked for the dashboard.

Each symbol is analysed as the live decision cycle analyses the traded one:
its history, the closed candles' indicators (``Analyzer._finalize_candle``),
``Analyzer.analyze`` and ``DecisionEngine.evaluate``. Only the market context
(options, news) is left out: the engine does not score it, and the news gate
belongs to the risk manager, which the bot checks when a trade is due.

Nothing here trades. A symbol is traded only after it is made the active
symbol, and then only on the bot's own signal. A scan reads history and
scores it with its own analyzer per symbol; it never reads or changes the
bot's active symbol, its open trade or its risk counters, so a scan (on the
15m schedule or asked for from the dashboard) cannot affect trading.
"""
import asyncio
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from analyzer import Analyzer
from backtest.replay import OFFLINE_CONTEXT
from decision_engine import DecisionEngine
from models.candle import Candle
from models.enums import Direction
from utils.helpers import session_bar_start
from utils.live_bars import in_session
from utils.market_structure import closed_candles
from utils.market_time import to_ist

#: The decision timeframe, as the engine trades it.
TIMEFRAME = "15m"
TIMEFRAME_MINUTES = 15
#: Closed candles given their indicators, newest last. A running engine has
#: them on every candle it sealed; the RSI divergence detector reads the RSI
#: of the last 5 (every other detector reads prices only).
FINALIZED_BARS = 5
#: Wait this long after a bar closes before asking for it, so the broker's
#: history has the bar.
SETTLE = timedelta(seconds=30)
#: Pause between history requests (the broker limits requests per second).
PACE_SECONDS = 0.25
#: How often the loop checks whether a new bar has closed.
CHECK_SECONDS = 15.0
#: When every symbol failed (broker down, token expired), try again after
#: this long rather than at once or only at the next bar.
RETRY_AFTER = timedelta(minutes=2)
#: Least time between two scans asked for from the dashboard: each scan makes
#: two history requests per watchlist symbol, on the broker quota the bot's
#: own orders share.
MANUAL_COOLDOWN = timedelta(seconds=60)

#: (symbol, timeframe) -> the warm-up history the engine analyzes
#: (analyzer.WARMUP_DAYS), oldest first.
FetchHistory = Callable[[str, str], Awaitable[List[Candle]]]
#: (symbol, name, instrument type) of every symbol to scan.
SymbolList = Callable[[], List[Tuple[str, str, str]]]


class ScanAnalyzer(Analyzer):
    """The live Analyzer without network calls (no market context)."""

    async def _get_market_context(self) -> Dict[str, Any]:
        return dict(OFFLINE_CONTEXT)


def scan_key(now: datetime) -> Any:
    """Changes once per closed 15m bar during the session (``SETTLE`` after
    it closes), and once per day outside it. ``now`` is naive UTC."""
    t = now - SETTLE
    if in_session(t):
        return session_bar_start(t, TIMEFRAME_MINUTES)
    return ("closed", to_ist(t).date())


def _rank(result: Dict[str, Any]) -> Tuple[int, int, str]:
    """Signals first, then the higher side's score; errors last."""
    if result.get("error"):
        return (2, 0, result["symbol"])
    best = max(result["long_score"], result["short_score"])
    return (0 if result["signal"] != Direction.WAIT.value else 1, -best, result["symbol"])


class Scanner:
    def __init__(
        self,
        fetch_history: FetchHistory,
        symbols: SymbolList,
        engine: DecisionEngine,
        pace_seconds: float = PACE_SECONDS,
    ):
        self._fetch = fetch_history
        self._symbols = symbols
        self._engine = engine
        self._pace = pace_seconds
        self._lock = asyncio.Lock()
        self._last_key: Any = None
        self._retry_at: Optional[datetime] = None
        self.results: List[Dict[str, Any]] = []
        self.scanned_at: Optional[datetime] = None
        self.scanning = False
        # A scan asked for by request_scan(), run by the run() loop.
        self._manual_requested = False
        self._last_manual_at: Optional[datetime] = None
        self._wake = asyncio.Event()

    async def scan_symbol(
        self, symbol: str, name: str, kind: str, now: datetime
    ) -> Dict[str, Any]:
        """The engine's verdict on ``symbol``'s newest closed 15m candle."""
        base = {"symbol": symbol, "name": name, "instrument_type": kind}
        try:
            candles_15m = closed_candles(
                await self._fetch(symbol, TIMEFRAME), TIMEFRAME_MINUTES, now
            )
            await asyncio.sleep(self._pace)
            # The higher-timeframe filter reads the 1h series as the live
            # engine holds it (the forming bar included).
            candles_1h = await self._fetch(symbol, "1h")
            await asyncio.sleep(self._pace)
            if not candles_15m:
                return {**base, "error": "No closed 15m candles"}

            analyzer = ScanAnalyzer()
            analyzer.candles_15m = candles_15m
            analyzer.candles_1h = candles_1h
            # Oldest first, each from the series up to it, as the engine
            # finalizes a candle when it closes.
            for i in range(max(0, len(candles_15m) - FINALIZED_BARS), len(candles_15m)):
                analyzer._finalize_candle(candles_15m[i], candles_15m[: i + 1])
            candle = candles_15m[-1]
            analysis = await analyzer.analyze(candle)
            signal = self._engine.evaluate(analysis)
            trend = analysis.get("higher_timeframe_trend")
            return {
                **base,
                "candle_time": candle.timestamp.isoformat(),
                "timeframe": TIMEFRAME,
                "price": candle.close,
                "long_score": self._engine._score_long(analysis),
                "short_score": self._engine._score_short(analysis),
                "signal": signal.direction.value,
                "score": signal.score,
                "reason": signal.reasoning,
                "higher_timeframe_trend": trend.value if trend is not None else None,
                "error": None,
            }
        except Exception as exc:
            return {**base, "error": str(exc) or exc.__class__.__name__}

    async def scan(self, now: Optional[datetime] = None) -> bool:
        """Scan every symbol once. False if a scan was already running."""
        if self._lock.locked():
            return False
        async with self._lock:
            self.scanning = True
            try:
                at = now or datetime.utcnow()
                results = []
                for symbol, name, kind in self._symbols():
                    results.append(await self.scan_symbol(symbol, name, kind, at))
                results.sort(key=_rank)
                self.results = results
                self.scanned_at = at
            finally:
                self.scanning = False
        return True

    async def scan_if_due(self, now: Optional[datetime] = None) -> bool:
        """Scan when a new bar has closed since the last scan (or never
        scanned). True if it scanned."""
        at = now or datetime.utcnow()
        key = scan_key(at)
        if key == self._last_key:
            return False
        if self._retry_at is not None and at < self._retry_at:
            return False
        if not await self.scan(at):
            return False
        if self.results and all(r.get("error") for r in self.results):
            self._retry_at = at + RETRY_AFTER
        else:
            self._last_key = key
            self._retry_at = None
        return True

    def request_scan(self, now: Optional[datetime] = None) -> Tuple[str, float]:
        """Ask the run loop for a scan now, outside the 15m schedule.

        Returns ("queued", 0), ("scanning", 0) when a scan is already running
        or queued, or ("cooldown", seconds left) within ``MANUAL_COOLDOWN`` of
        the previous request. The schedule is untouched: the next 15m close
        is still scanned.
        """
        at = now or datetime.utcnow()
        if self.scanning or self._manual_requested:
            return "scanning", 0.0
        if self._last_manual_at is not None:
            left = (self._last_manual_at + MANUAL_COOLDOWN - at).total_seconds()
            if left > 0:
                return "cooldown", left
        self._last_manual_at = at
        self._manual_requested = True
        self._wake.set()
        return "queued", 0.0

    async def run(self):
        """Scan at every 15m close while the engine runs, and when
        request_scan() asks (cancel to stop)."""
        while True:
            try:
                if self._manual_requested:
                    self._manual_requested = False
                    await self.scan()
                else:
                    await self.scan_if_due()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[SCANNER] Scan failed: {exc}")
            if not self._manual_requested:
                try:
                    await asyncio.wait_for(self._wake.wait(), CHECK_SECONDS)
                except asyncio.TimeoutError:
                    pass
            self._wake.clear()

    def snapshot(self) -> Dict[str, Any]:
        return {
            "scanned_at": self.scanned_at.isoformat() if self.scanned_at else None,
            "scanning": self.scanning or self._manual_requested,
            "timeframe": TIMEFRAME,
            "threshold": self._engine.min_threshold,
            "results": list(self.results),
        }
