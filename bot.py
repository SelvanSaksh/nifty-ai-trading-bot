import asyncio
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from fastapi import WebSocket

from brokers.fyers_broker_mock import FyersBroker

from analyzer import Analyzer
from decision_engine import DecisionEngine
from risk_manager import RiskManager
from database import save_candle, save_signal, save_trade, get_recent_trades, get_recent_signals
from notifier import TelegramNotifier
from models.candle import Candle
from models.signal import Signal
from models.trade import Trade
from models.enums import Direction, TradeStatus, SignalResult
from config import settings


class NiftyBot:
    """Main trading engine — tick loop, candle building, entry & exit."""
    
    def __init__(self):
        self.broker = FyersBroker()
        self.analyzer = Analyzer()
        self.decision_engine = DecisionEngine()
        self.risk_manager = RiskManager()
        self.notifier = TelegramNotifier()
        
        self.is_running = False
        self.start_time: Optional[datetime] = None
        self.active_trade: Optional[Trade] = None
        self.today_pnl = 0.0
        
        self.ws_clients: List[WebSocket] = []
        self._tick_task: Optional[asyncio.Task] = None
    
    @property
    def uptime(self) -> str:
        if not self.start_time:
            return "0:00:00"
        delta = datetime.now() - self.start_time
        return str(delta).split('.')[0]
    
    async def start(self):
        """Start the bot engine."""
        if self.is_running:
            return
        
        connected = await self.broker.connect()
        if not connected:
            raise ConnectionError("Failed to connect to Fyers")
        
        # Fetch historical candles so chart has data immediately
        try:
            historical_15m = await self.broker.get_historical_candles(
                symbol="NSE:NIFTY50-INDEX", timeframe="15", limit=200
            )
            self.analyzer.candles_15m = historical_15m
            print(f"[BOT] Loaded {len(historical_15m)} x 15m candles")
        except Exception as e:
            print(f"[BOT] Failed to load 15m candles: {e}")

        try:
            historical_1h = await self.broker.get_historical_candles(
                symbol="NSE:NIFTY50-INDEX", timeframe="60", limit=200
            )
            self.analyzer.candles_1h = historical_1h
            print(f"[BOT] Loaded {len(historical_1h)} x 1h candles")
        except Exception as e:
            print(f"[BOT] Failed to load 1h candles: {e}")

        try:
            historical_5m = await self.broker.get_historical_candles(
                symbol="NSE:NIFTY50-INDEX", timeframe="5", limit=200
            )
            self.analyzer.candles_5m = historical_5m
            print(f"[BOT] Loaded {len(historical_5m)} x 5m candles")
        except Exception as e:
            print(f"[BOT] Failed to load 5m candles: {e}")
        
        self.broker.tick_callback = self._on_tick
        await self.broker.subscribe_ticks(["NSE:NIFTY50-INDEX"])
        
        self.is_running = True
        self.start_time = datetime.now()
        await self.notifier.send("🚀 Nifty 50 AI Bot started (Paper: {})".format(settings.PAPER_TRADING))
    
    async def stop(self):
        """Stop the bot engine."""
        self.is_running = False
        await self.broker.disconnect()
        if self.active_trade:
            await self._emergency_exit("Bot stopped")
        await self.notifier.send("🛑 Nifty 50 AI Bot stopped")
    
    async def _on_tick(self, tick: dict):
        """Handle incoming tick from Fyers WebSocket."""
        if not self.is_running:
            return
        
        # Parse tick
        price = tick.get("ltp", 0)
        volume = tick.get("v", 0)
        timestamp = datetime.now()
        
        # Build candles
        closed_candle = self.analyzer.add_tick(price, volume, timestamp)
        
        # Broadcast live candle to Flutter clients
        await self._broadcast({
            "type": "tick",
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
    
    async def _on_candle_close(self, candle: Candle):
        """Full decision cycle on closed candle."""
        # Save candle to DB
        await save_candle(candle)
        
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
        analysis = self.analyzer.analyze(candle)
        
        # Decision engine
        signal = self.decision_engine.evaluate(analysis)
        
        # Save signal
        await save_signal(signal)
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
            f"Result: {trade.result.value}\n"
            f"Total PnL: {trade.realized_pnl:.2f}\n"
            f"Today's PnL: {self.today_pnl:.2f}"
        )
        await self.notifier.send(msg)
        await self._broadcast({"type": "trade_closed", "trade": trade.model_dump()})
        
        self.active_trade = None
    
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
            self.ws_clients.remove(client)
    
    def _candle_to_dict(self, candle: Candle) -> dict:
        return candle.model_dump()
    
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
    
    async def get_candles(self, timeframe: str, limit: int):
        if timeframe == "5m":
            candles = self.analyzer.candles_5m
        elif timeframe == "1h":
            candles = self.analyzer.candles_1h
        else:
            candles = self.analyzer.candles_15m
        return [c.model_dump() for c in candles[-limit:]]
    
    async def get_market_context(self):
        # NOW WITH await
        return await self.analyzer._get_market_context()