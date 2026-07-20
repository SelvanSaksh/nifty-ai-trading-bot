from datetime import datetime, time, timedelta
from typing import Optional, Tuple
from models.signal import Signal
from models.trade import Trade
from models.enums import Direction, TradeStatus
from config import settings
from features.news import news_monitor


class RiskManager:
    """Enforces pre-trade gates, validates signals, sizes positions."""
    
    def __init__(self):
        self.daily_pnl = 0.0
        self.weekly_pnl = 0.0
        self.trades_today = 0
        self.consecutive_losses = 0
        self.last_loss_time: Optional[datetime] = None
        self.cooldown_until: Optional[datetime] = None
        
        # News cache to avoid hitting RSS feeds every candle
        self._news_cache: Optional[Tuple[bool, list]] = None
        self._news_cache_time: Optional[datetime] = None
        self._news_cache_ttl = timedelta(seconds=60)
    
    async def can_trade(self, now: datetime = None) -> Tuple[bool, str]:
        """Pre-trade gate checks. Returns (can_trade, reason)."""
        if now is None:
            now = datetime.now()
        
        # ── 1. Session window ─────────────────────────────────────
        current_time = now.time()
        start = datetime.strptime(settings.TRADING_START_TIME, "%H:%M").time()
        end = datetime.strptime(settings.TRADING_END_TIME, "%H:%M").time()
        
        if now.weekday() >= 5:  # Saturday=5, Sunday=6
            return False, "Markets closed (weekend)"
        
        if not (start <= current_time <= end):
            return False, f"Outside trading hours ({settings.TRADING_START_TIME}-{settings.TRADING_END_TIME})"
        
        # ── 2. Daily loss limit ───────────────────────────────────
        daily_loss_limit = -settings.CAPITAL * settings.MAX_DAILY_LOSS
        if self.daily_pnl <= daily_loss_limit:
            return False, f"Daily loss limit hit: ₹{self.daily_pnl:,.2f} (limit: ₹{daily_loss_limit:,.2f})"
        
        # ── 3. Weekly loss limit ──────────────────────────────────
        weekly_loss_limit = -settings.CAPITAL * settings.MAX_WEEKLY_LOSS
        if self.weekly_pnl <= weekly_loss_limit:
            return False, f"Weekly loss limit hit: ₹{self.weekly_pnl:,.2f} (limit: ₹{weekly_loss_limit:,.2f})"
        
        # ── 4. Max trades per day ─────────────────────────────────
        if self.trades_today >= settings.MAX_TRADES_PER_DAY:
            return False, f"Max trades per day reached: {self.trades_today}/{settings.MAX_TRADES_PER_DAY}"
        
        # ── 5. Loss cooldown ──────────────────────────────────────
        if self.cooldown_until and now < self.cooldown_until:
            mins_left = int((self.cooldown_until - now).total_seconds() / 60)
            return False, f"Cooldown active: {mins_left} min remaining"
        
        # ── 6. News blackout (cached) ─────────────────────────────
        is_blackout, blackouts = await self._get_news_status()
        if is_blackout:
            titles = [b["title"][:50] + "..." for b in blackouts[:2]]
            return False, f"News blackout: {' | '.join(titles)}"
        
        return True, "OK"
    
    async def _get_news_status(self) -> Tuple[bool, list]:
        """Get news blackout status with caching."""
        if (self._news_cache_time is not None and 
            self._news_cache is not None and
            (datetime.now() - self._news_cache_time) < self._news_cache_ttl):
            return self._news_cache
        
        is_blackout = await news_monitor.is_blackout_active()
        blackouts = await news_monitor.get_active_blackouts() if is_blackout else []
        
        self._news_cache = (is_blackout, blackouts)
        self._news_cache_time = datetime.now()
        return self._news_cache
    
    def validate_signal(self, signal: Signal) -> Tuple[bool, str]:
        """Signal validation and risk-reward check."""
        if signal.direction == Direction.WAIT:
            return False, "WAIT signal"
        
        if signal.score < settings.MIN_SCORE_THRESHOLD:
            return False, f"Score {signal.score} below threshold {settings.MIN_SCORE_THRESHOLD}"
        
        if signal.risk_reward < settings.MIN_RISK_REWARD:
            return False, f"R:R {signal.risk_reward:.2f} below minimum {settings.MIN_RISK_REWARD}"
        
        # Stop loss validation: reject stops tighter than 0.8×ATR
        # (ATR reference would need to be passed in or stored)
        # risk = abs(signal.stop_loss - signal.target_1) / signal.risk_reward
        
        return True, "Signal valid"
    
    def calculate_position_size(self, signal: Signal, current_price: float) -> int:
        """Risk-based position sizing."""
        risk_amount = settings.CAPITAL * settings.RISK_PER_TRADE
        
        # Scale by confidence (50-150%)
        confidence_multiplier = 0.5 + signal.confidence  # 0.5 to 1.5
        
        # Scale by VIX regime (halved in extreme)
        # TODO: integrate with features/options.py vix_multiplier
        vix_multiplier = 1.0
        
        adjusted_risk = risk_amount * confidence_multiplier * vix_multiplier
        
        risk_per_unit = abs(current_price - signal.stop_loss)
        if risk_per_unit <= 0:
            return 0
        
        # Nifty lot size = 50
        lot_size = 50
        raw_qty = int(adjusted_risk / risk_per_unit)
        lots = max(1, raw_qty // lot_size)
        
        return lots * lot_size
    
    def update_after_trade(self, trade: Trade):
        """Update risk counters after trade close."""
        self.trades_today += 1
        self.daily_pnl += trade.realized_pnl
        self.weekly_pnl += trade.realized_pnl
        
        if trade.result and trade.result.value == "LOSS":
            self.consecutive_losses += 1
            self.last_loss_time = datetime.now()
            if self.consecutive_losses >= 2:
                self.cooldown_until = datetime.now() + timedelta(minutes=settings.COOLDOWN_MINUTES)
        else:
            self.consecutive_losses = 0
            self.cooldown_until = None
    
    def reset_daily(self):
        """Call at start of new trading day."""
        self.trades_today = 0
        self.daily_pnl = 0.0
        self.consecutive_losses = 0
        self.cooldown_until = None
        self._news_cache = None
        self._news_cache_time = None