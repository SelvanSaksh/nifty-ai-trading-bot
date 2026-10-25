import asyncio
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import WebSocket
from fastapi.encoders import jsonable_encoder

from brokers.base import ORDER_CANCELLED, ORDER_FILLED, ORDER_FINAL, ORDER_REJECTED
from brokers.fyers_broker import FyersBroker
from brokers.fyers_broker_mock import FyersBroker as MockFyersBroker

import trade_rules
from analyzer import Analyzer
from decision_engine import DecisionEngine
from risk_manager import RiskManager
from database import (
    save_candle,
    save_signal,
    save_trade,
    get_open_trade,
    get_recent_trades,
    get_recent_signals,
    get_active_symbol_state,
    set_active_symbol_state,
)
from notifier import TelegramNotifier
from models.candle import Candle
from models.signal import Signal
from models.trade import Trade
from models.enums import Direction, TradeStatus, SignalResult
from config import settings
from features.watchlist import watchlist_manager
from utils.engine_lock import EngineLockedError, SingletonFileLock
from utils.market_structure import DEFAULT_STRENGTH, StructureTracker, closed_candles
from utils.live_bars import LiveBarTracker, in_session
from utils.helpers import (
    LIVE_TIMEFRAMES,
    TIMEFRAME_MINUTES,
    RESOLUTION_BY_TIMEFRAME,
    normalize_timeframe,
    round_to_tick,
)

# EXCHANGE:CODE, e.g. NSE:NIFTY50-INDEX or NSE:INFY-EQ
SYMBOL_PATTERN = re.compile(r"^[A-Z]{2,12}:[A-Z0-9&\-\.]{1,40}$")


class SymbolValidationError(ValueError):
    """The requested symbol is malformed or not an available instrument."""


class SymbolConflictError(Exception):
    """The active trading symbol cannot change right now (open trade)."""


class NiftyBot:
    """Main trading engine — tick loop, candle building, entry & exit."""

    # History cache for candles of instruments other than the active one.
    HISTORY_LIMIT = 200
    HISTORY_CACHE_TTL = 60.0
    HISTORY_CACHE_MAX_ENTRIES = 32
    # Exchange time further ahead of the receive time than this is not trusted.
    EXCHANGE_CLOCK_SKEW = timedelta(seconds=60)
    # How often the engine checks the shared DB for symbol changes made by
    # another process (single writer, shared state).
    RECONCILE_INTERVAL = 5.0
    # Market structure: how often new candles are fetched per series, and
    # how many series stay in memory.
    STRUCTURE_REFRESH_SECONDS = 5.0
    STRUCTURE_MAX_SERIES = 24
    # A market order still open after this many polls is cancelled.
    ORDER_FILL_POLLS = 10
    ORDER_FILL_POLL_SECONDS = 0.5
    # Pause before an exit that did not go through is tried again.
    EXIT_RETRY_SECONDS = 10.0

    def __init__(self):
        mode = settings.BROKER_MODE.strip().lower()
        if mode == "fyers":
            self.broker = FyersBroker()
        elif mode == "mock":
            self.broker = MockFyersBroker()
        else:
            raise ValueError("BROKER_MODE must be 'fyers' or 'mock'")
        self.analyzer = Analyzer()
        self.decision_engine = DecisionEngine()
        self.risk_manager = RiskManager()
        self.notifier = TelegramNotifier()

        self.is_running = False
        self.start_time: Optional[datetime] = None
        self.active_trade: Optional[Trade] = None
        # Serializes every change to the open position. Ticks are handled
        # concurrently, and two handlers that both see the stop hit must not
        # both send an exit order.
        self._trade_lock = asyncio.Lock()
        # Why new trades are blocked (e.g. the broker position did not match
        # the bot's records), or None. Cleared by the next start.
        self.trading_halted: Optional[str] = None
        # The open trade's broker state is unknown: the bot stops touching it
        # until a restart reconciles it with the broker.
        self._trade_frozen = False
        self._exit_retry_at = 0.0  # time.monotonic()
        # Last traded price of the active symbol.
        self._last_price: Optional[float] = None

        self.symbol: str = settings.TRADING_SYMBOL
        self.symbol_name: str = settings.TRADING_SYMBOL_NAME
        # Version of the shared active-symbol state this engine has applied.
        self.symbol_version = 0
        self.symbol_changed_at: Optional[str] = None
        self.symbol_changed_by: str = "default"
        # Instrument the in-memory candle buffers actually belong to.
        self.analyzer_symbol: Optional[str] = None

        self.ws_clients: List[WebSocket] = []
        self._tick_task: Optional[asyncio.Task] = None

        # Serializes start/stop/symbol-switch so they can never interleave.
        self._lock = asyncio.Lock()
        # Guarantees a single engine process across workers/replicas.
        self._engine_lock = SingletonFileLock(Path(settings.DB_PATH).parent / "engine.lock")
        self.engine_leader = False
        self._reconcile_task: Optional[asyncio.Task] = None
        self._pending_symbol_state: Optional[Dict[str, Any]] = None
        self._history_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}
        # Forming candles of every subscribed symbol, from its ticks.
        self.live_bars = LiveBarTracker()
        # Last cumulative day volume per symbol: (UTC date, volume).
        self._last_day_volume: Dict[str, Tuple[Any, int]] = {}
        # Market structure per (symbol, timeframe, strength), fed incrementally.
        self._structure_cache: Dict[Tuple[str, str, int], Dict[str, Any]] = {}
        self._structure_locks: Dict[Tuple[str, str, int], asyncio.Lock] = {}

        # Multi-symbol watchlist live quotes: {symbol: {ltp, change, change_pct, open, high, low, volume, name, instrument_type}}
        self.watchlist_quotes: Dict[str, Dict[str, Any]] = {}
        self._all_symbols: List[str] = []

    @property
    def today_pnl(self) -> float:
        """Realized P&L of the current IST trading day (closed trades)."""
        return self.risk_manager.daily_pnl

    @property
    def uptime(self) -> str:
        if not self.start_time:
            return "0:00:00"
        delta = datetime.utcnow() - self.start_time
        return str(delta).split('.')[0]

    # ── Lifecycle ─────────────────────────────────────────────────

    async def start(self):
        """Start the bot engine (only one process may ever run it)."""
        async with self._lock:
            await self._start_locked()

    async def _start_locked(self):
        if self.is_running:
            return

        if not self._engine_lock.acquire():
            raise EngineLockedError(
                "Another process already runs the trading engine "
                f"(lock: {self._engine_lock.path}). Only one engine is allowed: "
                "multiple engines duplicate orders and desynchronize the active symbol."
            )
        self.engine_leader = True

        try:
            connected = await self.broker.connect()
            if not connected:
                raise ConnectionError("Failed to connect to Fyers")

            # Adopt the persisted active symbol (survives restarts) and load
            # history for *that* symbol.
            await self._sync_symbol_from_state()
            await self._load_history_for(self.symbol)
            # Limits and any trade left open survive restarts; the trade is
            # checked against the broker before anything new is allowed.
            await self.risk_manager.load()
            await self._restore_open_trade()

            self.broker.tick_callback = self._on_tick

            # Subscribe to ALL watchlist symbols for live watchlist data
            default_wl = watchlist_manager.create_default("default")
            self._all_symbols = [item.symbol for item in default_wl.items]
            if self.symbol not in self._all_symbols:
                self._all_symbols.append(self.symbol)
            await self.broker.subscribe_ticks(self._all_symbols)
            print(f"[BOT] Subscribed to {len(self._all_symbols)} symbols for watchlist")

            self.is_running = True
            self.start_time = datetime.utcnow()
            self._reconcile_task = asyncio.create_task(self._reconcile_loop())
        except BaseException:
            self.engine_leader = False
            self._engine_lock.release()
            raise

        await self.notifier.send(
            f"🚀 {self.symbol_name} AI Bot started for {self.symbol} "
            f"(Paper: {settings.PAPER_TRADING}, symbol v{self.symbol_version})"
        )

    async def stop(self):
        """Stop the bot engine."""
        async with self._lock:
            await self._stop_locked()

    async def _stop_locked(self):
        if self._reconcile_task is not None:
            self._reconcile_task.cancel()
            try:
                await self._reconcile_task
            except asyncio.CancelledError:
                pass
            self._reconcile_task = None

        self.is_running = False

        # Close the position while the broker session is still up (and
        # before another engine could take over and restore the trade).
        if self.active_trade:
            await self._emergency_exit("Bot stopped")

        if self.engine_leader:
            self._engine_lock.release()
            self.engine_leader = False

        await self.broker.disconnect()
        await self.notifier.send(f"🛑 {self.symbol_name} AI Bot stopped")

    async def reauthenticate(self):
        """Adopt a freshly stored Fyers token without restarting the engine.

        Called by the OAuth callback: the HTTP session and the data socket both
        cache the old credentials, so a new token only takes effect once they
        are rebuilt.
        """
        refresher = getattr(self.broker, "reauthenticate", None)
        if refresher is None:
            return
        # Serialized with start/stop and the live-feed watchdog, which would
        # otherwise rebuild the socket at the same moment.
        async with self._lock:
            await refresher()
            self.live_bars.reset_continuity()
            if self.is_running:
                self.broker.tick_callback = self._on_tick
                if self._all_symbols:
                    await self.broker.subscribe_ticks(self._all_symbols)
                    print(f"[BOT] Resubscribed to {len(self._all_symbols)} symbols after re-login")
                # While the token was dead no ticks arrived, so the tick-built
                # buffers miss every bar of the outage (often a whole session).
                await self._backfill_series("re-login")

    async def _backfill_series(self, reason: str):
        """Reload the engine's candle buffers from history after a feed gap.

        Caller holds ``self._lock``. A timeframe that fails to load keeps its
        current buffer: it is still better than none, and the next gap retries.
        """
        if self.analyzer_symbol != self.symbol:
            return
        try:
            bundle = await self._fetch_history_bundle(self.symbol)
        except Exception as exc:
            print(f"[BOT] Could not backfill candles after {reason}: {exc}")
            return
        for tf, attr in (("5m", "candles_5m"), ("15m", "candles_15m"), ("1h", "candles_1h")):
            if bundle[tf]:
                setattr(self.analyzer, attr, bundle[tf])
        print(f"[BOT] Backfilled candles for {self.symbol} after {reason}")

    # ── Active symbol ─────────────────────────────────────────────

    @staticmethod
    def _validate_symbol_format(symbol: str):
        if not symbol:
            raise SymbolValidationError("symbol is required")
        if not SYMBOL_PATTERN.match(symbol):
            raise SymbolValidationError(
                f"Invalid symbol '{symbol}'. Expected EXCHANGE:CODE, e.g. NSE:NIFTY50-INDEX"
            )

    @classmethod
    def _validate_tradable_symbol(cls, symbol: str):
        """Trading targets must be instruments we actually expose."""
        cls._validate_symbol_format(symbol)
        default_wl = watchlist_manager.create_default("default")
        if default_wl.get(symbol) is None:
            available = ", ".join(item.symbol for item in default_wl.items)
            raise SymbolValidationError(
                f"'{symbol}' is not in the watchlist. Available: {available}"
            )

    @staticmethod
    def _derive_symbol_name(symbol: str) -> str:
        default_wl = watchlist_manager.create_default("default")
        item = default_wl.get(symbol)
        if item is not None:
            return item.name
        name = symbol.split(":")[-1]
        for suffix in ("-INDEX", "-EQ"):
            if name.endswith(suffix):
                name = name[: -len(suffix)]
        return name

    def _symbol_name_for(self, symbol: str) -> str:
        if symbol == self.symbol:
            return self.symbol_name
        return self._derive_symbol_name(symbol)

    async def _sync_symbol_from_state(self):
        """Adopt the shared active-symbol state (no history load, no broadcast)."""
        state = await get_active_symbol_state()
        self.symbol = state["symbol"]
        self.symbol_name = state["symbol_name"]
        self.symbol_version = state["version"]
        self.symbol_changed_at = state["changed_at"]
        self.symbol_changed_by = state["changed_by"]

    async def _apply_symbol_state(
        self,
        state: Dict[str, Any],
        reason: str,
        broadcast: bool = True,
    ) -> bool:
        """
        Make this engine trade the symbol stored in ``state``.

        History for the new instrument is fetched *before* swapping, and the
        swap itself contains no ``await`` — so no tick can ever be applied to
        the wrong instrument's candle buffer.
        """
        new_symbol = state["symbol"]
        new_name = state["symbol_name"]
        if (
            new_symbol == self.symbol
            and new_name == self.symbol_name
            and state["version"] == self.symbol_version
        ):
            return False

        history: Optional[Dict[str, List[Candle]]] = None
        try:
            history = await self._fetch_history_bundle(new_symbol)
        except Exception as exc:
            print(f"[BOT] History load failed for {new_symbol}: {exc}")

        old_symbol = self.symbol
        self.symbol = new_symbol
        self.symbol_name = new_name
        self.symbol_version = state["version"]
        self.symbol_changed_at = state.get("changed_at")
        self.symbol_changed_by = state.get("changed_by", "system")

        if history is None:
            # Never keep another instrument's candles around.
            self.analyzer.candles_5m = []
            self.analyzer.candles_15m = []
            self.analyzer.candles_1h = []
        else:
            self.analyzer.candles_5m = history["5m"]
            self.analyzer.candles_15m = history["15m"]
            self.analyzer.candles_1h = history["1h"]
        self.analyzer_symbol = new_symbol
        self._history_cache.clear()

        if new_symbol not in self._all_symbols:
            self._all_symbols.append(new_symbol)

        if self.is_running:
            await self.broker.subscribe_ticks(self._all_symbols)

        if broadcast:
            await self._broadcast({
                "type": "symbol_changed",
                "old_symbol": old_symbol,
                "symbol": new_symbol,
                "symbol_name": new_name,
                "version": state["version"],
                "changed_by": state.get("changed_by", "system"),
                "reason": reason,
                "timestamp": datetime.now().isoformat(),
            })
            await self.notifier.send(
                f"🔁 Active symbol → {new_name} ({new_symbol}) "
                f"[v{state['version']}, {reason}]"
            )
        return True

    async def set_symbol(
        self,
        symbol: str,
        symbol_name: str = "",
        changed_by: str = "api",
        reason: str = "api_request",
    ) -> Dict[str, Any]:
        """
        Change the active trading symbol.

        The new value is persisted (single shared source of truth) *and*
        applied to this engine atomically under the lifecycle lock.
        Raises SymbolValidationError / SymbolConflictError on refusal.
        """
        target = (symbol or "").strip().upper()
        name = (symbol_name or "").strip() or self._derive_symbol_name(target)
        self._validate_tradable_symbol(target)
        if len(name) > 80:
            raise SymbolValidationError("symbol_name must be 80 characters or fewer")

        async with self._lock:
            current = await get_active_symbol_state()
            if target == current["symbol"] and name == current["symbol_name"]:
                return {
                    "message": "Symbol already active",
                    **current,
                    "in_sync": self.symbol_version == current["version"],
                    "engine_leader": self.engine_leader,
                    "bot_running": self.is_running,
                }

            if self.active_trade is not None:
                raise SymbolConflictError(
                    f"Cannot switch trading symbol while trade {self.active_trade.id} "
                    f"is open on {self.active_trade.symbol}. Close it first."
                )

            state = await set_active_symbol_state(
                target, name, updated_by=changed_by, reason=reason
            )
            await self._apply_symbol_state(state, reason=reason)

            return {
                "message": f"Symbol changed to {self.symbol_name}",
                **state,
                "in_sync": True,
                "engine_leader": self.engine_leader,
                "bot_running": self.is_running,
            }

    async def get_symbol(self) -> Dict[str, Any]:
        """
        Current active symbol, read from the shared state (identical answer in
        every process) plus this engine's sync status.
        """
        state = await get_active_symbol_state()
        in_sync = (not self.is_running) or (
            state["version"] == self.symbol_version and state["symbol"] == self.symbol
        )
        return {
            **state,
            "in_sync": in_sync,
            "engine_leader": self.engine_leader,
            "bot_running": self.is_running,
        }

    async def _reconcile_loop(self):
        """Apply active-symbol changes written by other processes/workers."""
        while True:
            await asyncio.sleep(self.RECONCILE_INTERVAL)
            try:
                await self._reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[BOT] Symbol reconcile failed: {exc}")
            try:
                await self._watch_live_feed()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[BOT] Live feed check failed: {exc}")
            try:
                await self._supervise_trade()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[BOT] Trade check failed: {exc}")

    async def _watch_live_feed(self):
        """Rebuild the broker's tick socket if it went silent in market hours."""
        ensure = getattr(self.broker, "ensure_live_feed", None)
        if ensure is None or not self.is_running:
            return
        async with self._lock:
            if self.is_running:
                if await ensure():
                    # Ticks were missed while the feed was silent.
                    self.live_bars.reset_continuity()
                    await self._backfill_series("feed rebuild")

    async def _reconcile_once(self) -> bool:
        if not self.is_running:
            return False

        state = await get_active_symbol_state()
        if state["version"] == self.symbol_version and state["symbol"] == self.symbol:
            return False

        async with self._lock:
            state = await get_active_symbol_state()
            if state["version"] == self.symbol_version and state["symbol"] == self.symbol:
                return False
            if self.active_trade is not None:
                # Defer instead of yanking the instrument out from a live trade.
                self._pending_symbol_state = state
                print(f"[BOT] Symbol switch deferred until {self.active_trade.id} closes")
                return False
            return await self._apply_symbol_state(state, reason="reconcile")

    async def _apply_pending_symbol(self, state: Dict[str, Any]):
        async with self._lock:
            if self.active_trade is not None:
                self._pending_symbol_state = state
                return
            self._pending_symbol_state = None
            try:
                await self._apply_symbol_state(state, reason="deferred_switch")
            except Exception as exc:
                print(f"[BOT] Deferred symbol switch failed: {exc}")

    # ── Candles ───────────────────────────────────────────────────

    def _series_for(self, tf: str) -> List[Candle]:
        if tf == "5m":
            return self.analyzer.candles_5m
        if tf == "15m":
            return self.analyzer.candles_15m
        return self.analyzer.candles_1h

    async def _load_history_for(self, symbol: str):
        """Replace all candle buffers with history for ``symbol``."""
        bundle = await self._fetch_history_bundle(symbol)
        self.analyzer.candles_5m = bundle["5m"]
        self.analyzer.candles_15m = bundle["15m"]
        self.analyzer.candles_1h = bundle["1h"]
        self.analyzer_symbol = symbol
        print(
            f"[BOT] Loaded {len(bundle['5m'])} x 5m, {len(bundle['15m'])} x 15m, "
            f"{len(bundle['1h'])} x 1h candles for {symbol}"
        )

    async def _fetch_history_bundle(self, symbol: str) -> Dict[str, List[Candle]]:
        bundle: Dict[str, List[Candle]] = {}
        for tf in ("5m", "15m", "1h"):
            try:
                bundle[tf] = await self._fetch_history(
                    symbol, tf, self.HISTORY_LIMIT, use_cache=False
                )
            except Exception as exc:
                print(f"[BOT] Failed to load {tf} candles for {symbol}: {exc}")
                bundle[tf] = []
        if not any(bundle.values()):
            raise RuntimeError(f"no history available for {symbol}")
        return bundle

    async def _fetch_history(
        self,
        symbol: str,
        tf: str,
        limit: int,
        end_time: Optional[datetime] = None,
        use_cache: bool = True,
    ) -> List[Candle]:
        cacheable = use_cache and end_time is None
        key = (symbol, tf)

        if cacheable:
            entry = self._history_cache.get(key)
            if (
                entry is not None
                and entry["expires"] > time.monotonic()
                and len(entry["candles"]) >= limit
                and entry["rollover"] == self._open_bar_after(entry["candles"], tf)
            ):
                return list(entry["candles"])

        # Taken before the request: ticks after this are not in the response.
        fetched_at = datetime.utcnow()
        volume_mark = self.live_bars.volume_mark(symbol)
        raw = await self.broker.get_historical_candles(
            symbol=symbol,
            timeframe=RESOLUTION_BY_TIMEFRAME[tf],
            limit=limit,
            end_time=end_time,
        )

        name = self._symbol_name_for(symbol)
        candles: List[Candle] = []
        for candle in raw:
            candle.symbol = symbol
            candle.symbol_name = name
            candle.timeframe = tf
            candles.append(candle)
        candles.sort(key=lambda c: c.timestamp)

        if cacheable:
            self._history_cache[key] = {
                "candles": candles,
                "expires": time.monotonic() + self.HISTORY_CACHE_TTL,
                "fetched_at": fetched_at,
                "volume_mark": volume_mark,
                # A bar that opens after the newest cached one is fetched for
                # once; if history does not have it yet, ticks form it meanwhile.
                "rollover": self._open_bar_after(candles, tf),
            }
            if len(self._history_cache) > self.HISTORY_CACHE_MAX_ENTRIES:
                oldest = min(self._history_cache, key=lambda k: self._history_cache[k]["expires"])
                self._history_cache.pop(oldest, None)

        return candles

    @staticmethod
    def _open_bar_after(candles: List[Candle], tf: str) -> Optional[datetime]:
        """Start of the bar forming now, if it opened after the newest of
        ``candles`` (during the session only; otherwise None)."""
        now = datetime.utcnow()
        if not candles or not in_session(now):
            return None
        step = timedelta(minutes=TIMEFRAME_MINUTES[tf])
        last = candles[-1].timestamp
        if now < last + step:
            return None
        return last + ((now - last) // step) * step

    def _with_live_bar(self, symbol: str, tf: str, candles: List[Candle]) -> List[Candle]:
        """History of ``symbol`` with its forming bar brought up to date from
        ticks received since the history was fetched."""
        entry = self._history_cache.get((symbol, tf))
        if entry is None or not candles:
            return candles
        return self.live_bars.apply(
            symbol,
            candles,
            timedelta(minutes=TIMEFRAME_MINUTES[tf]),
            entry["fetched_at"],
            entry["volume_mark"],
            datetime.utcnow(),
        )

    async def get_candles(
        self,
        timeframe: str = "15m",
        limit: int = 100,
        symbol: Optional[str] = None,
        end_time: Any = None,
    ) -> Dict[str, Any]:
        """
        Candles for a specific instrument.

        Without ``symbol`` the shared active symbol is used — but the response
        always states which symbol the candles belong to, so a client can never
        render data under the wrong label again.
        """
        tf = normalize_timeframe(timeframe)
        limit = int(limit)
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        end_dt = self._parse_end_time(end_time)

        active_state = await get_active_symbol_state()
        if symbol:
            target = str(symbol).strip().upper()
            self._validate_symbol_format(target)
            target_name = self._symbol_name_for(target)
        else:
            target = active_state["symbol"]
            target_name = active_state["symbol_name"]

        candles: List[Candle] = []
        source = "history"
        error: Optional[str] = None

        live_ok = (
            target == self.symbol
            and self.analyzer_symbol == target
            and tf in LIVE_TIMEFRAMES
        )
        if live_ok:
            live = list(self._series_for(tf))
            # Live buffers only help if they reach back to the requested window.
            if len(live) >= limit and (end_dt is None or live[0].timestamp <= end_dt):
                candles, source = live, "live"

        if not candles or (end_dt is not None and candles[0].timestamp > end_dt):
            try:
                fetch_limit = limit if end_dt is not None else max(limit, self.HISTORY_LIMIT)
                candles = await self._fetch_history(target, tf, fetch_limit, end_time=end_dt)
                if end_dt is None:
                    candles = self._with_live_bar(target, tf, candles)
                source = "history"
            except Exception as exc:
                error = str(exc)
                candles = []
                source = "history"

        if end_dt is not None:
            candles = [c for c in candles if c.timestamp <= end_dt]
        if candles:
            candles = candles[-limit:]

        payload = [
            {
                **c.model_dump(),
                "symbol": target,
                "symbol_name": target_name,
                "timeframe": tf,
            }
            for c in candles
        ]

        return {
            "symbol": target,
            "symbol_name": target_name,
            "symbol_version": active_state["version"],
            "active_symbol": active_state["symbol"],
            "timeframe": tf,
            "limit": limit,
            "source": source,
            "count": len(payload),
            "coverage": (
                {"start": payload[0]["timestamp"], "end": payload[-1]["timestamp"]}
                if payload
                else None
            ),
            "error": error,
            "candles": payload,
        }

    #: Widest window one /api/candles/range call may ask for.
    MAX_RANGE_DAYS = 400

    async def get_candles_range(
        self,
        symbol: str,
        timeframe: str,
        start: Any,
        end: Any = None,
    ) -> Dict[str, Any]:
        """Every candle of ``symbol`` between ``start`` and ``end`` (default:
        now) - no candle-count limit. Charts page older history with it."""
        tf = normalize_timeframe(timeframe)
        target = str(symbol or "").strip().upper()
        self._validate_symbol_format(target)
        start_dt = self._parse_end_time(start)
        if start_dt is None:
            raise ValueError("start is required")
        end_dt = self._parse_end_time(end) or datetime.utcnow()
        if end_dt < start_dt:
            raise ValueError("end must not be before start")
        if end_dt - start_dt > timedelta(days=self.MAX_RANGE_DAYS):
            raise ValueError(f"a range may span at most {self.MAX_RANGE_DAYS} days")

        active_state = await get_active_symbol_state()
        target_name = self._symbol_name_for(target)
        error: Optional[str] = None
        candles: List[Candle] = []
        try:
            candles = await self.broker.get_history_range(
                target, RESOLUTION_BY_TIMEFRAME[tf], start_dt, end_dt
            )
        except Exception as exc:
            error = str(exc)

        payload = [
            {**c.model_dump(), "symbol": target, "symbol_name": target_name, "timeframe": tf}
            for c in candles
        ]
        return {
            "symbol": target,
            "symbol_name": target_name,
            "symbol_version": active_state["version"],
            "active_symbol": active_state["symbol"],
            "timeframe": tf,
            "limit": len(payload),
            "source": "history",
            "count": len(payload),
            "coverage": (
                {"start": payload[0]["timestamp"], "end": payload[-1]["timestamp"]}
                if payload
                else None
            ),
            "error": error,
            "candles": payload,
        }

    def _structure_start(self) -> datetime:
        """Session open (09:15 IST = 03:45 UTC) on ``STRUCTURE_START``."""
        day = datetime.fromisoformat(str(settings.STRUCTURE_START).strip()[:10])
        return day.replace(hour=3, minute=45, second=0, microsecond=0)

    async def get_structure(
        self,
        symbol: str,
        timeframe: str,
        since: Any = None,
        strength: int = DEFAULT_STRENGTH,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Market structure (swings, BOS/CHoCH, zones) of every closed candle
        since ``STRUCTURE_START`` - no fixed candle count.

        The start never moves, so a confirmed swing, BOS or CHoCH can never
        change afterwards. Each series is kept in memory and only new closed
        candles are fetched and fed in. The candle still forming is left out;
        the chart previews it against ``watch``. ``since`` only trims which
        swings and breaks are returned (what the chart can show).
        """
        strength = int(strength)
        if strength < 1 or strength > 10:
            raise ValueError("strength must be between 1 and 10")
        tf = normalize_timeframe(timeframe)
        target = str(symbol or "").strip().upper()
        self._validate_symbol_format(target)
        since_dt = self._parse_end_time(since)
        now = now or datetime.utcnow()

        key = (target, tf, strength)
        lock = self._structure_locks.setdefault(key, asyncio.Lock())
        error: Optional[str] = None
        async with lock:
            entry = self._structure_cache.pop(key, None) or {
                "tracker": StructureTracker(strength),
                "fetched": None,
            }
            tracker: StructureTracker = entry["tracker"]
            fetched = entry["fetched"]
            if fetched is None or time.monotonic() - fetched >= self.STRUCTURE_REFRESH_SECONDS:
                start = tracker.last_time or self._structure_start()
                try:
                    raw = await self.broker.get_history_range(
                        target, RESOLUTION_BY_TIMEFRAME[tf], start, now
                    )
                    tracker.feed(closed_candles(raw, TIMEFRAME_MINUTES[tf], now))
                    entry["fetched"] = time.monotonic()
                except Exception as exc:
                    error = str(exc)
            # Most recently used last; the oldest series is dropped first.
            self._structure_cache[key] = entry
            while len(self._structure_cache) > self.STRUCTURE_MAX_SERIES:
                oldest = next(iter(self._structure_cache))
                self._structure_cache.pop(oldest)
                self._structure_locks.pop(oldest, None)
            structure = tracker.result()

        return {
            "symbol": target,
            "symbol_name": self._symbol_name_for(target),
            "timeframe": tf,
            "start": self._structure_start().isoformat(),
            "error": error,
            "summary": structure.summary(),
            **structure.to_dict(since_dt),
        }

    @staticmethod
    def _parse_end_time(value: Any) -> Optional[datetime]:
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            dt = value
        else:
            text = str(value).strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            try:
                dt = datetime.fromisoformat(text)
            except ValueError:
                raise ValueError(
                    f"Invalid end_time '{value}'. Expected ISO-8601, e.g. 2026-09-27T15:30:00"
                )
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt

    # ── Tick handling ─────────────────────────────────────────────

    async def _on_tick(self, tick: dict):
        """Handle incoming tick from Fyers WebSocket."""
        tick_symbol = tick.get("symbol", self.symbol)
        price = tick.get("ltp", 0)
        volume = tick.get("v", 0)
        # Candles are stored as naïve UTC by API contract. Never use the
        # host's local clock here: that shifts bar boundaries and indicators.
        received_at = datetime.utcnow()
        # The trade's own exchange time places it in its bar, as other charts
        # do; the receive time is only a fallback (missing/implausible value).
        exchange_at = self._exchange_time(tick, received_at)
        timestamp = exchange_at or received_at
        
        # ``v`` is the day's cumulative traded volume, not this tick's.
        traded = self._traded_since_last_tick(tick_symbol, volume, received_at)

        # Always update watchlist quotes regardless of bot state
        self._update_watchlist_quote(tick_symbol, price, volume, timestamp)
        # Forming candles of every symbol (history-served series use them).
        self.live_bars.record(tick_symbol, price, volume, received_at, exchange_at)
        
        # Broadcast watchlist tick to all clients
        quote = self.watchlist_quotes.get(tick_symbol, {})
        await self._broadcast({
            "type": "watchlist_tick",
            "symbol": tick_symbol,
            "ltp": price,
            "change": quote.get("change", 0),
            "change_pct": quote.get("change_pct", 0),
            "open": quote.get("open", price),
            "high": quote.get("high", price),
            "low": quote.get("low", price),
            "volume": volume,
            "timestamp": timestamp.isoformat(),
        })
        
        # Only process candle building / trade logic for the active trading symbol when running
        if not self.is_running or tick_symbol != self.symbol:
            return
        self._last_price = price
        
        # Build candles from the quantity traded since the previous tick.
        # Fyers keeps sending index ticks after the close; bars built from
        # them are flat phantoms that make the series look current while the
        # real session is missing, so only session ticks form candles.
        closed_candle = (
            self.analyzer.add_tick(price, traded, timestamp)
            if in_session(timestamp)
            else None
        )
        
        # Broadcast live candle to Flutter clients (existing single-symbol chart data)
        await self._broadcast({
            "type": "tick",
            "symbol": self.symbol,
            "symbol_version": self.symbol_version,
            "price": price,
            "timestamp": timestamp.isoformat(),
            "live_candle": self._candle_to_dict(self.analyzer.candles_15m[-1]) if self.analyzer.candles_15m else None
        })
        
        # If active trade, manage it every tick
        if self.active_trade:
            await self._manage_trade(price, timestamp)
        
        # If candle closed, run full analysis
        if closed_candle:
            await self._on_candle_close(closed_candle)
    
    def _traded_since_last_tick(self, symbol: str, day_volume: Any, at: datetime) -> int:
        """Quantity traded since ``symbol``'s previous tick, from the day's
        cumulative volume the feed reports."""
        day_volume = int(day_volume or 0)
        previous = self._last_day_volume.get(symbol)
        self._last_day_volume[symbol] = (at.date(), day_volume)
        if previous is None:
            # The current bar comes from history, which already holds the
            # volume traded so far; only later trades are added.
            return 0
        day, last = previous
        if day != at.date():
            # First tick of a new session: all of today's volume is new.
            return day_volume
        return max(0, day_volume - last)

    @classmethod
    def _exchange_time(cls, tick: dict, received_at: datetime) -> Optional[datetime]:
        """The tick's exchange time as naive UTC, or None if absent/implausible."""
        raw = tick.get("exch_time")
        try:
            at = datetime.utcfromtimestamp(int(raw)) if raw else None
        except (TypeError, ValueError, OverflowError, OSError):
            return None
        if at is None or at > received_at + cls.EXCHANGE_CLOCK_SKEW:
            return None
        return at

    def _update_watchlist_quote(self, symbol: str, price: float, volume: int, timestamp: datetime):
        """Update the live quote for a symbol in the watchlist."""
        if symbol in self.watchlist_quotes:
            q = self.watchlist_quotes[symbol]
            q["ltp"] = price
            q["high"] = max(q.get("high", price), price)
            q["low"] = min(q.get("low", price), price)
            q["volume"] = volume
            q["timestamp"] = timestamp.isoformat()
            # Change from open
            open_p = q.get("open", price)
            q["change"] = round(price - open_p, 2)
            q["change_pct"] = round((price - open_p) / open_p * 100, 2) if open_p else 0
        else:
            self.watchlist_quotes[symbol] = {
                "symbol": symbol,
                "ltp": price,
                "open": price,
                "high": price,
                "low": price,
                "change": 0,
                "change_pct": 0,
                "volume": volume,
                "timestamp": timestamp.isoformat(),
            }
    
    async def _on_candle_close(self, candle: Candle):
        """Full decision cycle on closed candle."""
        # Save candle to DB
        await save_candle(candle, self.symbol)

        # Check if trading allowed — NOW WITH await
        can_trade, reason = await self.risk_manager.can_trade()
        if not can_trade:
            await self._broadcast({
                "type": "skip",
                "reason": reason,
                "timestamp": datetime.now().isoformat()
            })
            return

        # Run analysis
        analysis = await self.analyzer.analyze(candle)
        # Use the same structure the chart shows (every candle since
        # STRUCTURE_START) rather than the analyzer's short live buffer.
        try:
            shared = await self.get_structure(self.symbol, "15m")
            if not shared["error"]:
                analysis["market_structure"] = shared["summary"]
        except Exception as exc:
            print(f"[BOT] Market structure unavailable: {exc}")

        # Decision engine
        signal = self.decision_engine.evaluate(analysis)
        signal.symbol = self.symbol
        signal.symbol_name = self.symbol_name

        # Save signal
        await save_signal(signal, self.symbol, self.symbol_name)
        await self._broadcast({
            "type": "signal",
            "signal": signal.model_dump()
        })

        # Validate and enter if passes
        valid, validation_reason = self.risk_manager.validate_signal(signal)
        if valid and signal.direction != Direction.WAIT:
            validation_reason = await self._enter_trade(signal, candle.close)
            if validation_reason is None:
                return
        await self._broadcast({
            "type": "wait",
            "reason": validation_reason,
            "signal_score": signal.score
        })

    # ── Trades ────────────────────────────────────────────────────
    #
    # Paper trades never reach the broker. A live trade is an intraday
    # market entry plus a stop-loss market (SL-M) order resting at the
    # broker for the open quantity, so the position stays protected even if
    # this process dies. Every exit goes to the broker, and the bot only
    # books what the broker confirms as filled.
    #
    # Methods below the public entry points (_enter_trade, _manage_trade,
    # _supervise_trade, _emergency_exit) expect the caller to hold
    # ``self._trade_lock``.

    @staticmethod
    def _entry_side(trade: Trade) -> int:
        return 1 if trade.direction == Direction.LONG else -1

    @classmethod
    def _exit_side(cls, trade: Trade) -> int:
        return -cls._entry_side(trade)

    async def _enter_trade(self, signal: Signal, entry_price: float) -> Optional[str]:
        """Enter a new trade. Returns why it was not entered, or None."""
        async with self._trade_lock:
            # One position at a time: a second entry would leave the first
            # one untracked (no stop, no exit).
            if self.active_trade is not None:
                return f"Trade {self.active_trade.id} is already open"
            if self.trading_halted:
                return f"New trades blocked: {self.trading_halted}"
            paper = settings.PAPER_TRADING
            if not paper and not self.symbol.endswith("-EQ"):
                return (
                    f"{self.symbol} cannot be traded live: only NSE equities (-EQ) "
                    "are supported; an index has no orders"
                )

            lot = self.risk_manager.lot_size_for(self.symbol)
            qty = self.risk_manager.calculate_position_size(signal, entry_price, lot)
            if qty <= 0:
                return "Position size 0: one lot would risk more than the per-trade limit"

            trade = Trade(
                # Naive UTC, like every time the API serves.
                entry_time=datetime.utcnow(),
                direction=signal.direction,
                entry_price=entry_price,
                quantity=qty,
                initial_qty=qty,
                remaining_qty=qty,
                # On the exchange price step, so the bot and the broker stop
                # trigger at the same price.
                stop_loss=round_to_tick(signal.stop_loss),
                target_1=signal.target_1,
                target_2=signal.target_2,
                target_3=signal.target_3,
                symbol=self.symbol,
                symbol_name=self.symbol_name,
                signal_score=signal.score,
                confidence=signal.confidence,
                paper_trade=paper
            )

            if not paper and not await self._open_live_position(trade):
                return "Entry order did not fill (see alerts)"

            self.active_trade = trade
            await save_trade(trade)

            msg = (
                f"📊 {'PAPER' if paper else 'LIVE'} TRADE ENTERED\n"
                f"Symbol: {trade.symbol_name} ({trade.symbol})\n"
                f"Direction: {trade.direction.value}\n"
                f"Entry: {trade.entry_price}\n"
                f"Qty: {trade.quantity}\n"
                f"SL: {trade.stop_loss}\n"
                f"T1: {trade.target_1} | T2: {trade.target_2} | T3: {trade.target_3}\n"
                f"Score: {trade.signal_score}/100 | Confidence: {trade.confidence:.0%}"
            )
            await self.notifier.send(msg)
            await self._broadcast({"type": "trade_entered", "trade": trade.model_dump()})

            if not paper and trade.sl_order_id is None:
                # Never hold a position the broker is not protecting.
                await self.notifier.send(
                    f"🚨 The broker refused the stop-loss of {trade.id} — closing the position"
                )
                await self._exit_trade(trade.entry_price, datetime.utcnow(), "STOP_ORDER_FAILED")
            return None

    async def _open_live_position(self, trade: Trade) -> bool:
        """Send the entry and its broker stop. False if no position opened."""
        result = await self._market_fill(trade.symbol, self._entry_side(trade), trade.quantity, trade)
        if result is None:
            await self._freeze(f"the entry order of {trade.id} is in an unknown state")
            return False
        filled, price = result
        if filled <= 0:
            await self.notifier.send(f"⚠️ Entry order for {trade.symbol} did not fill; no trade opened")
            return False
        trade.quantity = trade.initial_qty = trade.remaining_qty = filled
        if price:
            trade.entry_price = price
        await self._place_stop(trade, filled)
        return True

    async def _market_fill(
        self, symbol: str, side: int, qty: int, trade: Optional[Trade] = None
    ) -> Optional[Tuple[int, Optional[float]]]:
        """Market order, waited on until it is filled or finished.

        Returns (filled quantity, average price); (0, None) when the broker
        refused it; None when its state could not be learned (it may or may
        not have filled).
        """
        reply = await self.broker.place_market_order(symbol, side, qty)
        order_id = str(reply.get("id") or "")
        if reply.get("s") != "ok" or not order_id:
            await self.notifier.send(f"⚠️ Order for {qty} {symbol} refused: {reply.get('message')}")
            return 0, None
        if trade is not None and trade.entry_order_id is None:
            trade.entry_order_id = order_id
        return await self._await_fill(order_id, qty)

    async def _await_fill(self, order_id: str, qty: int) -> Optional[Tuple[int, Optional[float]]]:
        order = None
        for _ in range(self.ORDER_FILL_POLLS):
            order = await self.broker.get_order(order_id)
            if order is not None and order["status"] in ORDER_FINAL:
                break
            await asyncio.sleep(self.ORDER_FILL_POLL_SECONDS)
        else:
            # Still working: cancel what is left, then read what did fill
            # (a fill can land just before the cancel).
            await self.broker.cancel_order(order_id)
            order = await self.broker.get_order(order_id)
        if order is None or order["status"] not in ORDER_FINAL:
            return None
        filled = order["filled_qty"]
        if order["status"] == ORDER_FILLED and filled <= 0:
            filled = qty
        return filled, order["price"]

    async def _place_stop(self, trade: Trade, qty: int) -> bool:
        """Rest a stop-loss order at the broker for ``qty`` of ``trade``."""
        reply = await self.broker.place_stop_order(
            trade.symbol, self._exit_side(trade), qty, trade.stop_loss
        )
        order_id = str(reply.get("id") or "")
        trade.sl_order_id = None
        if reply.get("s") != "ok" or not order_id:
            print(f"[BOT] Stop order for {trade.id} refused: {reply.get('message')}")
            return False
        # Accepted orders can still be rejected by the exchange a moment later.
        order = await self.broker.get_order(order_id)
        if order is not None and order["status"] in (ORDER_CANCELLED, ORDER_REJECTED):
            print(f"[BOT] Stop order for {trade.id} {order['status'].lower()}: {order['message']}")
            return False
        trade.sl_order_id = order_id
        return True

    async def _protect(self, trade: Trade, open_qty: int):
        """Make the broker stop cover exactly ``open_qty`` again."""
        if open_qty <= 0:
            return
        if trade.sl_order_id:
            ok = await self.broker.modify_order(
                trade.sl_order_id, qty=open_qty, type=3, stopPrice=trade.stop_loss
            )
        else:
            ok = await self._place_stop(trade, open_qty)
        if not ok:
            await self.notifier.send(
                f"🚨 {trade.id}: {open_qty} {trade.symbol} open with NO broker stop-loss"
            )

    async def _broker_stop_fill(self, trade: Trade) -> Optional[float]:
        """Fill price if the broker's stop already closed the position."""
        if trade.paper_trade or not trade.sl_order_id:
            return None
        order = await self.broker.get_order(trade.sl_order_id)
        if order is None or order["status"] != ORDER_FILLED:
            return None
        return order["price"] or trade.stop_loss

    async def _execute_exit(
        self, trade: Trade, qty: int, price: float
    ) -> Optional[Tuple[int, float, bool]]:
        """Take ``qty`` off the open position.

        Returns (quantity closed, fill price, closed by the broker stop), or
        None when nothing was closed (the position is unchanged; retry later).
        """
        if trade.paper_trade:
            return qty, price, False

        stop_fill = await self._broker_stop_fill(trade)
        if stop_fill is not None:
            return trade.remaining_qty, stop_fill, True

        keep = trade.remaining_qty - qty
        # Shrink the broker stop to what stays open *before* selling: a stop
        # bigger than the position would open a reverse position if it fired.
        if trade.sl_order_id:
            if keep > 0:
                adjusted = await self.broker.modify_order(
                    trade.sl_order_id, qty=keep, type=3, stopPrice=trade.stop_loss
                )
            else:
                adjusted = await self.broker.cancel_order(trade.sl_order_id)
            if not adjusted:
                # Most likely the stop fired a moment ago.
                stop_fill = await self._broker_stop_fill(trade)
                if stop_fill is not None:
                    return trade.remaining_qty, stop_fill, True
                await self.notifier.send(f"⚠️ Could not adjust the stop of {trade.id}; exit postponed")
                return None
            if keep == 0:
                trade.sl_order_id = None

        result = await self._market_fill(trade.symbol, self._exit_side(trade), qty)
        if result is None:
            await self._freeze(f"an exit order of {trade.id} is in an unknown state")
            return None
        filled, fill_price = result
        if filled < qty:
            await self._protect(trade, trade.remaining_qty - filled)
            if filled <= 0:
                await self.notifier.send(f"⚠️ Exit order for {trade.id} did not fill; will retry")
                return None
        return filled, fill_price or price, False

    async def _manage_trade(self, current_price: float, timestamp: datetime):
        """Manage open position — square-off, stop loss and targets
        (the rules live in trade_rules, shared with the backtester)."""
        async with self._trade_lock:
            trade = self.active_trade
            if trade is None or self._trade_frozen or time.monotonic() < self._exit_retry_at:
                return
            action = trade_rules.next_exit(trade, current_price, timestamp)
            if action is None:
                return
            if action.fraction is None:
                await self._exit_trade(action.price, timestamp, action.reason)
            else:
                await self._partial_exit(action.price, timestamp, action.new_status, action.fraction)

    async def _supervise_trade(self):
        """Checks that must not wait for a tick (the feed can fall silent):
        the square-off, and that the broker stop is still in place."""
        if self.active_trade is None:
            return
        async with self._trade_lock:
            trade = self.active_trade
            if trade is None or self._trade_frozen or time.monotonic() < self._exit_retry_at:
                return
            now = datetime.utcnow()
            if trade_rules.square_off_due(trade, now):
                # A paper trade needs a real price; a live one is filled by the broker.
                price = self._last_price or (None if trade.paper_trade else trade.entry_price)
                if price is not None:
                    await self._exit_trade(price, now, "EOD_SQUARE_OFF")
                return
            if not trade.paper_trade:
                await self._check_broker_stop(trade)

    async def _check_broker_stop(self, trade: Trade):
        if trade.sl_order_id is None:
            # An earlier (re)placement failed; keep trying.
            if await self._place_stop(trade, trade.remaining_qty):
                await save_trade(trade)
                await self.notifier.send(f"🛡️ Stop-loss for {trade.id} is in place at the broker")
            return
        order = await self.broker.get_order(trade.sl_order_id)
        if order is None:
            return
        if order["status"] == ORDER_FILLED:
            await self._finish_trade(order["price"] or trade.stop_loss, datetime.utcnow(), "STOP_LOSS")
        elif order["status"] in (ORDER_CANCELLED, ORDER_REJECTED):
            await self.notifier.send(
                f"🚨 Broker stop of {trade.id} was {order['status'].lower()} "
                f"({order['message'] or 'no reason given'}); placing a new one"
            )
            await self._place_stop(trade, trade.remaining_qty)
            await save_trade(trade)

    async def _partial_exit(self, price: float, timestamp: datetime, new_status: TradeStatus, fraction: float):
        """Execute partial exit at target (in whole lots)."""
        trade = self.active_trade
        label = "T1" if new_status == TradeStatus.PARTIAL_1 else "T2"
        exit_qty = trade_rules.partial_qty(
            trade, fraction, self.risk_manager.lot_size_for(trade.symbol)
        )

        if exit_qty <= 0:
            # Too small to split: the whole position rides to the next target.
            trade.status = new_status
            await save_trade(trade)
            await self.notifier.send(
                f"🎯 {label} HIT at {price}; {trade.remaining_qty} is too small to split, holding it all"
            )
            await self._broadcast({"type": "partial_exit", "trade": trade.model_dump()})
            return

        done = await self._execute_exit(trade, exit_qty, price)
        if done is None:
            self._exit_retry_at = time.monotonic() + self.EXIT_RETRY_SECONDS
            await save_trade(trade)
            return
        exit_qty, price, stopped = done
        if stopped:
            await self._finish_trade(price, timestamp, "STOP_LOSS")
            return

        pnl = self._calculate_pnl(trade, price, exit_qty)
        trade.realized_pnl += pnl
        trade.remaining_qty -= exit_qty

        if new_status == TradeStatus.PARTIAL_1:
            trade.exit_t1_price = price
            trade.exit_t1_qty = exit_qty
            trade.exit_t1_time = timestamp
        else:
            trade.exit_t2_price = price
            trade.exit_t2_qty = exit_qty
            trade.exit_t2_time = timestamp
        trade.status = new_status
        await self.notifier.send(f"🎯 {label} HIT! Exited {exit_qty} at {price}. PnL: {pnl:.2f}")

        await save_trade(trade)
        await self._broadcast({"type": "partial_exit", "trade": trade.model_dump()})

    async def _exit_trade(self, price: float, timestamp: datetime, reason: str):
        """Close remaining position (stays open if the broker did not fill)."""
        trade = self.active_trade
        done = await self._execute_exit(trade, trade.remaining_qty, price)
        if done is None:
            self._exit_retry_at = time.monotonic() + self.EXIT_RETRY_SECONDS
            await save_trade(trade)
            return
        exit_qty, price, stopped = done
        if stopped:
            reason = "STOP_LOSS"
        if exit_qty < trade.remaining_qty:
            # Only part of the exit filled: book that part, retry the rest.
            trade.realized_pnl += self._calculate_pnl(trade, price, exit_qty)
            trade.remaining_qty -= exit_qty
            await save_trade(trade)
            self._exit_retry_at = time.monotonic() + self.EXIT_RETRY_SECONDS
            return
        await self._finish_trade(price, timestamp, reason)

    async def _finish_trade(self, price: float, timestamp: datetime, reason: str):
        """Book the close of everything still open at ``price``."""
        trade = self.active_trade
        exit_qty = trade.remaining_qty

        pnl = self._calculate_pnl(trade, price, exit_qty)
        trade.realized_pnl += pnl
        trade.remaining_qty = 0
        trade.status = TradeStatus.CLOSED
        trade.exit_price = price
        trade.exit_time = timestamp
        trade.exit_reason = reason

        trade.result = SignalResult.WIN if trade.realized_pnl > 0 else SignalResult.LOSS

        if reason == "STOP_LOSS":
            trade.exit_sl_price = price
            trade.exit_sl_time = timestamp
        elif reason == "TARGET_3":
            trade.exit_t3_price = price
            trade.exit_t3_qty = exit_qty
            trade.exit_t3_time = timestamp

        # Update risk manager (saved, so a restart cannot reset the limits)
        self.risk_manager.update_after_trade(trade)
        await self.risk_manager.save()

        await save_trade(trade)

        emoji = "✅" if trade.result.value == "WIN" else "❌"
        msg = (
            f"{emoji} TRADE CLOSED ({reason})\n"
            f"Symbol: {trade.symbol_name} ({trade.symbol})\n"
            f"Result: {trade.result.value}\n"
            f"Total PnL: {trade.realized_pnl:.2f}\n"
            f"Today's PnL: {self.today_pnl:.2f}"
        )
        await self.notifier.send(msg)
        await self._broadcast({"type": "trade_closed", "trade": trade.model_dump()})

        self.active_trade = None
        self._exit_retry_at = 0.0

        # Apply a symbol switch that was deferred while this trade was open.
        if self._pending_symbol_state is not None:
            pending = self._pending_symbol_state
            self._pending_symbol_state = None
            asyncio.ensure_future(self._apply_pending_symbol(pending))

    def _calculate_pnl(self, trade: Trade, exit_price: float, qty: int) -> float:
        if trade.direction == Direction.LONG:
            return (exit_price - trade.entry_price) * qty
        else:
            return (trade.entry_price - exit_price) * qty

    async def _emergency_exit(self, reason: str):
        """Emergency exit when bot stops (at the last traded price)."""
        async with self._trade_lock:
            trade = self.active_trade
            if trade is None:
                return
            price = self._last_price or trade.entry_price
            await self._exit_trade(price, datetime.utcnow(), f"EMERGENCY: {reason}")
            if self.active_trade is not None:
                await self.notifier.send(
                    f"🚨 {trade.id} is STILL OPEN after '{reason}' — "
                    "check the Fyers account (the broker stop-loss stays in place)"
                )

    # ── Restart recovery ──────────────────────────────────────────

    async def _halt(self, reason: str):
        """Block new trades until the next start."""
        self.trading_halted = reason
        print(f"[BOT] New trades blocked: {reason}")
        await self.notifier.send(f"⛔ New trades blocked: {reason}")

    async def _freeze(self, reason: str):
        """Stop touching the open trade: its broker state is unknown."""
        self._trade_frozen = True
        await self._halt(f"{reason} — check the Fyers account, then restart the bot")

    async def _broker_net_qty(self, symbol: str) -> int:
        positions = await self.broker.get_positions()
        return sum(
            p["net_qty"]
            for p in positions
            if p["symbol"] == symbol and (p.get("product") or "INTRADAY") == "INTRADAY"
        )

    async def _restore_open_trade(self):
        """Pick up a trade an earlier run left open (crash, restart) and make
        sure the broker agrees before any new trade is allowed."""
        self.trading_halted = None
        self._trade_frozen = False
        self._exit_retry_at = 0.0
        self.active_trade = None
        trade = await get_open_trade()

        if trade is not None and not trade.paper_trade:
            live_symbol = trade.symbol
        elif not settings.PAPER_TRADING:
            live_symbol = self.symbol
        else:
            live_symbol = None

        broker_qty = 0
        if live_symbol is not None:
            try:
                broker_qty = await self._broker_net_qty(live_symbol)
            except Exception as exc:
                await self._halt(f"could not read broker positions to check {live_symbol}: {exc}")
                return

        if trade is None:
            if broker_qty:
                await self._halt(
                    f"the broker shows {broker_qty} {live_symbol} open that the bot has no record of"
                )
            return

        if trade.symbol != self.symbol:
            await self._halt(
                f"open trade {trade.id} is on {trade.symbol} but the active symbol is {self.symbol}"
            )
            return

        if trade.paper_trade:
            self.active_trade = trade
            print(f"[BOT] Restored open paper trade {trade.id}")
            return

        expected = self._entry_side(trade) * trade.remaining_qty
        if broker_qty == expected:
            self.active_trade = trade
            await self.notifier.send(f"♻️ Restored open trade {trade.id}; the broker position matches")
            return

        if broker_qty == 0:
            # Closed while the bot was away: the broker stop, or the broker's
            # own end-of-day square-off.
            self.active_trade = trade
            stop_fill = await self._broker_stop_fill(trade)
            if stop_fill is not None:
                await self._finish_trade(stop_fill, datetime.utcnow(), "STOP_LOSS")
                return
            # A stop still resting at the broker would, if it fired, open a
            # position the other way.
            if trade.sl_order_id and not await self.broker.cancel_order(trade.sl_order_id):
                await self.notifier.send(
                    f"🚨 Could not cancel stop order {trade.sl_order_id} of closed trade "
                    f"{trade.id} — cancel it in the Fyers account"
                )
            # Exit price unknown: P&L stays at what was booked, the result is
            # left empty, and the limits are not touched.
            trade.remaining_qty = 0
            trade.status = TradeStatus.CLOSED
            trade.exit_reason = "CLOSED_OUTSIDE_BOT"
            trade.exit_time = datetime.utcnow()
            await save_trade(trade)
            self.active_trade = None
            await self.notifier.send(
                f"⚠️ {trade.id} was closed outside the bot (exit price unknown); "
                "check its P&L in the Fyers account"
            )
            return

        await self._halt(
            f"broker position {broker_qty} {trade.symbol} does not match trade {trade.id} ({expected})"
        )

    # ── WebSocket broadcast ───────────────────────────────────────
    def add_websocket_client(self, ws: WebSocket):
        self.ws_clients.append(ws)
    
    def remove_websocket_client(self, ws: WebSocket):
        if ws in self.ws_clients:
            self.ws_clients.remove(ws)
    
    async def _broadcast(self, data: dict):
        # Messages carry datetimes and enums (trades, signals, candles); plain
        # json.dumps raised on them, and the client was dropped as if it had
        # disconnected.
        data = jsonable_encoder(data)
        disconnected = []
        for client in self.ws_clients:
            try:
                await client.send_json(data)
            except:
                disconnected.append(client)
        for client in disconnected:
            if client in self.ws_clients:
                self.ws_clients.remove(client)
    
    def _candle_to_dict(self, candle: Candle) -> dict:
        return {
            **candle.model_dump(),
            "symbol": candle.symbol or self.symbol,
            "symbol_name": candle.symbol_name or self.symbol_name,
        }
    
    # ── MCP / API helpers ─────────────────────────────────────────
    async def get_trading_stats(self) -> dict:
        trades = await get_recent_trades(1000)
        wins = sum(1 for t in trades if t.result and t.result.value == "WIN")
        total = len([t for t in trades if t.result])
        win_rate = (wins / total * 100) if total > 0 else 0
        total_pnl = sum(t.realized_pnl for t in trades)
        
        return {
            "win_rate": round(win_rate, 2),
            "total_trades": total,
            "total_pnl": round(total_pnl, 2),
            "today_pnl": round(self.today_pnl, 2),
            "wins": wins,
            "losses": total - wins
        }
    
    async def get_recent_trades(self, limit: int = 20):
        return await get_recent_trades(limit)
    
    async def get_recent_signals(self, limit: int = 20):
        return await get_recent_signals(limit)
    
    async def get_market_context(self):
        # NOW WITH await
        return await self.analyzer._get_market_context()
    
    def get_watchlist_quotes(self) -> list:
        """Return live quotes for all watchlist symbols."""
        default_wl = watchlist_manager.create_default("default")
        result = []
        for item in default_wl.items:
            q = self.watchlist_quotes.get(item.symbol, {})
            result.append({
                "symbol": item.symbol,
                "name": item.name,
                "instrument_type": item.instrument_type,
                "exchange": item.exchange,
                "ltp": q.get("ltp", 0),
                "change": q.get("change", 0),
                "change_pct": q.get("change_pct", 0),
                "open": q.get("open", 0),
                "high": q.get("high", 0),
                "low": q.get("low", 0),
                "volume": q.get("volume", 0),
                "is_active": item.symbol == self.symbol,
            })
        return result
