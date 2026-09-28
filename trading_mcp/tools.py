"""
MCP (Model Context Protocol) Tool Definitions for the Trading Bot.
Exposes 10 structured tools for AI assistant integration.
"""
from typing import Dict, Any, List, Optional
from datetime import datetime

from features.portfolio import portfolio_tracker, Timeframe
from features.journal_ai import trade_journal


class MCPTools:
    """
    MCP Tool implementations.
    Each method corresponds to one tool exposed by the MCP server.
    """
    
    def __init__(self):
        self.bot_instance = None  # Set by server.py on mount
    
    def set_bot(self, bot):
        """Link to the running NiftyBot instance."""
        self.bot_instance = bot
    
    async def invoke(self, tool_name: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """Route tool invocation to the correct handler."""
        params = params or {}
        
        handlers = {
            "get_bot_status": self.get_bot_status,
            "get_trading_stats": self.get_trading_stats,
            "get_recent_trades": self.get_recent_trades,
            "get_recent_signals": self.get_recent_signals,
            "get_candles": self.get_candles,
            "get_market_context": self.get_market_context,
            "get_config_summary": self.get_config_summary,
            "explain_latest_signal": self.explain_latest_signal,
            "start_bot": self.start_bot,
            "stop_bot": self.stop_bot,
        }
        
        handler = handlers.get(tool_name)
        if not handler:
            return {
                "success": False,
                "error": f"Unknown tool: {tool_name}",
                "available_tools": list(handlers.keys())
            }
        
        try:
            result = await handler(**params)
            return {"success": True, "data": result}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    # ── Tool 1: get_bot_status ──────────────────────────────────
    async def get_bot_status(self) -> Dict[str, Any]:
        """Running state, uptime, active trade, paper/live mode."""
        if not self.bot_instance:
            return {"error": "Bot not initialized"}
        
        return {
            "is_running": self.bot_instance.is_running,
            "uptime": self.bot_instance.uptime,
            "start_time": self.bot_instance.start_time.isoformat() if self.bot_instance.start_time else None,
            "active_trade": self._trade_to_dict(self.bot_instance.active_trade),
            "paper_mode": self.bot_instance.active_trade.paper_trade if self.bot_instance.active_trade else True,
            "today_pnl": round(self.bot_instance.today_pnl, 2),
            "symbol": self.bot_instance.symbol,
            "symbol_name": self.bot_instance.symbol_name,
            "websocket_clients": len(self.bot_instance.ws_clients),
        }
    
    # ── Tool 2: get_trading_stats ───────────────────────────────
    async def get_trading_stats(self, timeframe: str = "all") -> Dict[str, Any]:
        """Win rate, total P&L, today's P&L."""
        if not self.bot_instance:
            return {"error": "Bot not initialized"}
        
        # Get from portfolio tracker
        tf_map = {
            "today": Timeframe.TODAY,
            "week": Timeframe.WEEK,
            "month": Timeframe.MONTH,
            "year": Timeframe.YEAR,
            "all": Timeframe.ALL,
        }
        tf = tf_map.get(timeframe, Timeframe.ALL)
        
        metrics = portfolio_tracker.get_metrics(tf)
        
        return {
            "timeframe": timeframe,
            "win_rate": metrics.win_rate,
            "total_trades": metrics.total_trades,
            "winning_trades": metrics.winning_trades,
            "losing_trades": metrics.losing_trades,
            "avg_win": metrics.avg_win,
            "avg_loss": metrics.avg_loss,
            "profit_factor": metrics.profit_factor,
            "max_drawdown": metrics.max_drawdown,
            "sharpe_ratio": metrics.sharpe_ratio,
            "today_pnl": round(self.bot_instance.today_pnl, 2),
        }
    
    # ── Tool 3: get_recent_trades ───────────────────────────────
    async def get_recent_trades(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Recent closed trades from the journal."""
        if not self.bot_instance:
            return []
        
        trades = await self.bot_instance.get_recent_trades(limit)
        return [self._trade_to_dict(t) for t in trades]
    
    # ── Tool 4: get_recent_signals ──────────────────────────────
    async def get_recent_signals(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Recent signals with confidence and reasoning."""
        if not self.bot_instance:
            return []
        
        signals = await self.bot_instance.get_recent_signals(limit)
        return signals
    
    # ── Tool 5: get_candles ─────────────────────────────────────
    async def get_candles(
        self,
        timeframe: str = "15m",
        limit: int = 50,
        symbol: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        OHLCV candles for chart analysis.

        Returns an envelope that always states which symbol the candles belong
        to — pass ``symbol`` to inspect an instrument other than the active one.
        """
        if not self.bot_instance:
            return {"error": "Bot not initialized"}
        
        return await self.bot_instance.get_candles(
            timeframe=timeframe, limit=limit, symbol=symbol
        )
    
    # ── Tool 6: get_market_context ──────────────────────────────
    async def get_market_context(self) -> Dict[str, Any]:
        """Options PCR, OI shifts, VIX regime."""
        if not self.bot_instance:
            return {"error": "Bot not initialized"}
        
        return await self.bot_instance.get_market_context()
    
    # ── Tool 7: get_config_summary ──────────────────────────────
    async def get_config_summary(self) -> Dict[str, Any]:
        """Key configuration values (safe, no secrets)."""
        from config import settings
        
        return {
            "paper_trading": settings.PAPER_TRADING,
            "capital": settings.CAPITAL,
            "risk_per_trade": settings.RISK_PER_TRADE,
            "max_daily_loss": settings.MAX_DAILY_LOSS,
            "max_weekly_loss": settings.MAX_WEEKLY_LOSS,
            "max_trades_per_day": settings.MAX_TRADES_PER_DAY,
            "min_score_threshold": settings.MIN_SCORE_THRESHOLD,
            "min_risk_reward": settings.MIN_RISK_REWARD,
            "primary_timeframe": settings.PRIMARY_TIMEFRAME,
            "higher_timeframe": settings.HIGHER_TIMEFRAME,
            "trading_hours": f"{settings.TRADING_START_TIME} - {settings.TRADING_END_TIME}",
            "cooldown_minutes": settings.COOLDOWN_MINUTES,
            "news_blackout_minutes": settings.NEWS_BLACKOUT_MINUTES,
        }
    
    # ── Tool 8: explain_latest_signal ───────────────────────────
    async def explain_latest_signal(self) -> Dict[str, Any]:
        """Human-readable rationale for the latest signal."""
        if not self.bot_instance:
            return {"error": "Bot not initialized"}
        
        signals = await self.bot_instance.get_recent_signals(1)
        if not signals:
            return {"message": "No signals generated yet"}
        
        latest = signals[0]
        
        return {
            "timestamp": latest.get("timestamp"),
            "direction": latest.get("direction"),
            "score": latest.get("score"),
            "confidence": latest.get("confidence"),
            "reasoning": latest.get("reasoning"),
            "risk_reward": latest.get("risk_reward"),
            "stop_loss": latest.get("stop_loss"),
            "targets": {
                "t1": latest.get("target_1"),
                "t2": latest.get("target_2"),
                "t3": latest.get("target_3"),
            },
            "ht_filter_passed": latest.get("ht_filter_passed"),
        }
    
    # ── Tool 9: start_bot ───────────────────────────────────────
    async def start_bot(self, confirm: bool = False) -> Dict[str, Any]:
        """Start the engine (explicit request only)."""
        if not confirm:
            return {
                "success": False,
                "message": "This is a state-changing operation. Set confirm=true to proceed.",
                "warning": "Starting the bot will begin live market analysis and potential trading."
            }
        
        if not self.bot_instance:
            return {"success": False, "error": "Bot not initialized"}
        
        if self.bot_instance.is_running:
            return {"success": False, "message": "Bot is already running"}
        
        await self.bot_instance.start()
        return {
            "success": True,
            "message": "Bot started successfully",
            "started_at": datetime.now().isoformat(),
            "mode": "paper" if self.bot_instance.active_trade and self.bot_instance.active_trade.paper_trade else "live"
        }
    
    # ── Tool 10: stop_bot ───────────────────────────────────────
    async def stop_bot(self, confirm: bool = False) -> Dict[str, Any]:
        """Stop the engine (explicit request only)."""
        if not confirm:
            return {
                "success": False,
                "message": "This is a state-changing operation. Set confirm=true to proceed.",
                "warning": "Stopping the bot will halt all trading activity. Open positions will be managed until manually closed."
            }
        
        if not self.bot_instance:
            return {"success": False, "error": "Bot not initialized"}
        
        if not self.bot_instance.is_running:
            return {"success": False, "message": "Bot is not running"}
        
        await self.bot_instance.stop()
        return {
            "success": True,
            "message": "Bot stopped successfully",
            "stopped_at": datetime.now().isoformat(),
            "today_pnl": round(self.bot_instance.today_pnl, 2)
        }
    
    # ── Bonus: Portfolio tools ──────────────────────────────────
    async def get_portfolio_summary(self) -> Dict[str, Any]:
        """Get portfolio snapshot and equity curve."""
        snapshot = portfolio_tracker.take_snapshot()
        equity_curve = portfolio_tracker.get_equity_curve()
        
        return {
            "snapshot": snapshot.to_dict(),
            "equity_curve": equity_curve[-30:],  # Last 30 points
            "trade_distribution": portfolio_tracker.get_trade_distribution()
        }
    
    async def get_journal_analysis(self) -> Dict[str, Any]:
        """Get AI-powered journal pattern analysis."""
        return trade_journal.get_pattern_analysis()
    
    # ── Helper methods ──────────────────────────────────────────
    def _trade_to_dict(self, trade) -> Optional[Dict[str, Any]]:
        """Safely convert trade to dict."""
        if not trade:
            return None
        
        return {
            "id": trade.id,
            "direction": trade.direction.value if hasattr(trade.direction, 'value') else str(trade.direction),
            "entry_price": trade.entry_price,
            "entry_time": trade.entry_time.isoformat() if trade.entry_time else None,
            "quantity": trade.quantity,
            "remaining_qty": trade.remaining_qty,
            "status": trade.status.value if hasattr(trade.status, 'value') else str(trade.status),
            "stop_loss": trade.stop_loss,
            "target_1": trade.target_1,
            "target_2": trade.target_2,
            "target_3": trade.target_3,
            "realized_pnl": round(trade.realized_pnl, 2),
            "result": trade.result.value if trade.result and hasattr(trade.result, 'value') else None,
            "signal_score": trade.signal_score,
            "confidence": trade.confidence,
        }