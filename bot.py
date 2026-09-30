import asyncio
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import WebSocket

from brokers.fyers_broker import FyersBroker
from brokers.fyers_broker_mock import FyersBroker as MockFyersBroker

from analyzer import Analyzer
from decision_engine import DecisionEngine
from risk_manager import RiskManager
from database import (
    save_candle,
    save_signal,
    save_trade,
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
from utils.helpers import (
    LIVE_TIMEFRAMES,
    RESOLUTION_BY_TIMEFRAME,
    normalize_timeframe,
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
    # How often the engine checks the shared DB for symbol changes made by
    # another process (single writer, shared state).
    RECONCILE_INTERVAL = 5.0

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
        self.today_pnl = 0.0

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

        # Multi-symbol watchlist live quotes: {symbol: {ltp, change, change_pct, open, high, low, volume, name, instrument_type}}
        self.watchlist_quotes: Dict[str, Dict[str, Any]] = {}
        self._all_symbols: List[str] = []

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

        if self.engine_leader:
            self._engine_lock.release()
            self.engine_leader = False

        await self.broker.disconnect()
        if self.active_trade:
            await self._emergency_exit("Bot stopped")
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
        await refresher()
        if self.is_running:
            self.broker.tick_callback = self._on_tick
            if self._all_symbols:
                await self.broker.subscribe_ticks(self._all_symbols)
                print(f"[BOT] Resubscribed to {len(self._all_symbols)} symbols after re-login")

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
            ):
                return list(entry["candles"])

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
            }
            if len(self._history_cache) > self.HISTORY_CACHE_MAX_ENTRIES:
                oldest = min(self._history_cache, key=lambda k: self._history_cache[k]["expires"])
                self._history_cache.pop(oldest, None)

        return candles

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
        timestamp = datetime.utcnow()
        
        # Always update watchlist quotes regardless of bot state
        self._update_watchlist_quote(tick_symbol, price, volume, timestamp)
        
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
        
        # Build candles
        closed_candle = self.analyzer.add_tick(price, volume, timestamp)
        
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
            await self._enter_trade(signal, candle.close)
        else:
            await self._broadcast({
                "type": "wait",
                "reason": validation_reason,
                "signal_score": signal.score
            })
    
    async def _enter_trade(self, signal: Signal, entry_price: float):
        """Enter a new trade."""
        qty = self.risk_manager.calculate_position_size(signal, entry_price)
        if qty <= 0:
            return
        
        trade = Trade(
            entry_time=datetime.now(),
            direction=signal.direction,
            entry_price=entry_price,
            quantity=qty,
            initial_qty=qty,
            remaining_qty=qty,
            stop_loss=signal.stop_loss,
            target_1=signal.target_1,
            target_2=signal.target_2,
            target_3=signal.target_3,
            symbol=self.symbol,
            symbol_name=self.symbol_name,
            signal_score=signal.score,
            confidence=signal.confidence,
            paper_trade=settings.PAPER_TRADING
        )
        
        # Place order
        order_result = await self.broker.place_order(trade)
        
        if order_result.get("s") == "ok":
            self.active_trade = trade
            await save_trade(trade)
            
            msg = (
                f"📊 {'PAPER' if settings.PAPER_TRADING else 'LIVE'} TRADE ENTERED\n"
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
    
    async def _manage_trade(self, current_price: float, timestamp: datetime):
        """Manage open position — stop loss and targets."""
        if not self.active_trade:
            return
        
        trade = self.active_trade
        
        # Check stop loss first (highest priority)
        sl_hit = (
            (trade.direction == Direction.LONG and current_price <= trade.stop_loss) or
            (trade.direction == Direction.SHORT and current_price >= trade.stop_loss)
        )
        
        if sl_hit:
            await self._exit_trade(trade.stop_loss, timestamp, "STOP_LOSS")
            return
        
        # Check targets
        if trade.status == TradeStatus.OPEN:
            t1_hit = (
                (trade.direction == Direction.LONG and current_price >= trade.target_1) or
                (trade.direction == Direction.SHORT and current_price <= trade.target_1)
            )
            if t1_hit:
                await self._partial_exit(trade.target_1, timestamp, TradeStatus.PARTIAL_1, 0.5)
        
        elif trade.status == TradeStatus.PARTIAL_1:
            t2_hit = (
                (trade.direction == Direction.LONG and current_price >= trade.target_2) or
                (trade.direction == Direction.SHORT and current_price <= trade.target_2)
            )
            if t2_hit:
                await self._partial_exit(trade.target_2, timestamp, TradeStatus.PARTIAL_2, 0.375)
        
        elif trade.status == TradeStatus.PARTIAL_2:
            t3_hit = (
                (trade.direction == Direction.LONG and current_price >= trade.target_3) or
                (trade.direction == Direction.SHORT and current_price <= trade.target_3)
            )
            if t3_hit:
                await self._exit_trade(trade.target_3, timestamp, "TARGET_3")
    
    async def _partial_exit(self, price: float, timestamp: datetime, new_status: TradeStatus, fraction: float):
        """Execute partial exit at target."""
        trade = self.active_trade
        exit_qty = int(trade.initial_qty * fraction)
        exit_qty = min(exit_qty, trade.remaining_qty)
        
        pnl = self._calculate_pnl(trade, price, exit_qty)
        trade.realized_pnl += pnl
        trade.remaining_qty -= exit_qty
        
        if new_status == TradeStatus.PARTIAL_1:
            trade.exit_t1_price = price
            trade.exit_t1_qty = exit_qty
            trade.exit_t1_time = timestamp
            trade.status = TradeStatus.PARTIAL_1
            await self.notifier.send(f"🎯 T1 HIT! Exited 50% at {price}. PnL: {pnl:.2f}")
        
        elif new_status == TradeStatus.PARTIAL_2:
            trade.exit_t2_price = price
            trade.exit_t2_qty = exit_qty
            trade.exit_t2_time = timestamp
            trade.status = TradeStatus.PARTIAL_2
            await self.notifier.send(f"🎯 T2 HIT! Exited 30% at {price}. PnL: {pnl:.2f}")
        
        await save_trade(trade)
        await self._broadcast({"type": "partial_exit", "trade": trade.model_dump()})
    
    async def _exit_trade(self, price: float, timestamp: datetime, reason: str):
        """Close remaining position."""
        trade = self.active_trade
        exit_qty = trade.remaining_qty
        
        pnl = self._calculate_pnl(trade, price, exit_qty)
        trade.realized_pnl += pnl
        trade.remaining_qty = 0
        trade.status = TradeStatus.CLOSED
        
        trade.result = SignalResult.WIN if trade.realized_pnl > 0 else SignalResult.LOSS
        
        if reason == "STOP_LOSS":
            trade.exit_sl_price = price
            trade.exit_sl_time = timestamp
        elif reason == "TARGET_3":
            trade.exit_t3_price = price
            trade.exit_t3_qty = exit_qty
            trade.exit_t3_time = timestamp
        
        # Update risk manager
        self.risk_manager.update_after_trade(trade)
        self.today_pnl += trade.realized_pnl
        
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
        """Emergency exit when bot stops."""
        if self.active_trade:
            await self._exit_trade(self.active_trade.entry_price, datetime.now(), f"EMERGENCY: {reason}")
    
    # ── WebSocket broadcast ───────────────────────────────────────
    def add_websocket_client(self, ws: WebSocket):
        self.ws_clients.append(ws)
    
    def remove_websocket_client(self, ws: WebSocket):
        if ws in self.ws_clients:
            self.ws_clients.remove(ws)
    
    async def _broadcast(self, data: dict):
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
