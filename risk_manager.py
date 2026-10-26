from datetime import datetime, time, timedelta
from typing import Any, Dict, Optional, Tuple
from models.signal import Signal
from models.trade import Trade
from models.enums import Direction, TradeStatus
from config import settings
from features.news import news_monitor
from database import get_app_state, set_app_state
from utils.market_time import ist_now, parse_hhmm

#: app_state key the counters are saved under, so a restart cannot reset them.
RISK_STATE_KEY = "risk_state"


class RiskManager:
    """Enforces pre-trade gates, validates signals, sizes positions.

    Every time here is IST wall-clock time (naive), whatever the server's
    own zone is.
    """

    def __init__(self):
        self.daily_pnl = 0.0
        self.weekly_pnl = 0.0
        self.trades_today = 0
        self.consecutive_losses = 0
        self.last_loss_time: Optional[datetime] = None
        self.cooldown_until: Optional[datetime] = None
        # IST trading day ("2026-10-26") and ISO week ("2026-W44") the
        # counters belong to; None until the first roll-over.
        self.day: Optional[str] = None
        self.week: Optional[str] = None

        # News cache to avoid hitting RSS feeds every candle
        self._news_cache: Optional[Tuple[bool, list]] = None
        self._news_cache_time: Optional[datetime] = None
        self._news_cache_ttl = timedelta(seconds=60)

    # ── Persistence ───────────────────────────────────────────────

    def to_state(self) -> Dict[str, Any]:
        return {
            "day": self.day,
            "week": self.week,
            "daily_pnl": self.daily_pnl,
            "weekly_pnl": self.weekly_pnl,
            "trades_today": self.trades_today,
            "consecutive_losses": self.consecutive_losses,
            "last_loss_time": self.last_loss_time.isoformat() if self.last_loss_time else None,
            "cooldown_until": self.cooldown_until.isoformat() if self.cooldown_until else None,
        }

    def _apply_state(self, state: Dict[str, Any]):
        def _time(value):
            return datetime.fromisoformat(value) if value else None

        self.day = state.get("day")
        self.week = state.get("week")
        self.daily_pnl = float(state.get("daily_pnl") or 0.0)
        self.weekly_pnl = float(state.get("weekly_pnl") or 0.0)
        self.trades_today = int(state.get("trades_today") or 0)
        self.consecutive_losses = int(state.get("consecutive_losses") or 0)
        self.last_loss_time = _time(state.get("last_loss_time"))
        self.cooldown_until = _time(state.get("cooldown_until"))

    async def load(self, now: datetime = None):
        """Restore the saved counters, then start a new day/week if one began."""
        saved = await get_app_state(RISK_STATE_KEY)
        if saved is not None:
            self._apply_state(saved["value"])
        if self.roll_over(now or ist_now()):
            await self.save()

    async def save(self):
        await set_app_state(RISK_STATE_KEY, self.to_state(), updated_by="risk_manager")

    def roll_over(self, now: datetime) -> bool:
        """Reset the daily counters on a new IST day and the weekly P&L on a
        new ISO week. True if anything changed (the caller saves)."""
        day = now.date().isoformat()
        year, week, _ = now.isocalendar()
        week_key = f"{year}-W{week:02d}"
        changed = False
        if self.week != week_key:
            if self.week is not None:
                self.weekly_pnl = 0.0
            self.week = week_key
            changed = True
        if self.day != day:
            if self.day is not None:
                self.reset_daily()
            self.day = day
            changed = True
        return changed

    # ── Limits ────────────────────────────────────────────────────

    @property
    def daily_loss_limit(self) -> float:
        """Daily P&L (₹, negative) at or below which no new trade opens."""
        return -settings.CAPITAL * settings.MAX_DAILY_LOSS

    @property
    def weekly_loss_limit(self) -> float:
        """Weekly P&L (₹, negative) at or below which no new trade opens."""
        return -settings.CAPITAL * settings.MAX_WEEKLY_LOSS

    @property
    def max_trades_per_day(self) -> int:
        """Closed trades per day after which no new trade opens."""
        return settings.MAX_TRADES_PER_DAY

    # ── Gates ─────────────────────────────────────────────────────

    async def can_trade(self, now: datetime = None) -> Tuple[bool, str]:
        """Pre-trade gate checks. Returns (can_trade, reason).

        ``now`` is IST wall-clock time (default: the current IST time).
        """
        if now is None:
            now = ist_now()
        if self.roll_over(now):
            await self.save()

        # ── 1. Session window ─────────────────────────────────────
        # New trades stop at the square-off time: one opened later would be
        # closed again straight away.
        current_time = now.time()
        start = parse_hhmm(settings.TRADING_START_TIME)
        end = min(parse_hhmm(settings.TRADING_END_TIME), parse_hhmm(settings.SQUARE_OFF_TIME))

        if now.weekday() >= 5:  # Saturday=5, Sunday=6
            return False, "Markets closed (weekend)"

        if not (start <= current_time < end):
            return False, f"Outside trading hours ({settings.TRADING_START_TIME}-{end.strftime('%H:%M')} IST)"

        # ── 2. Daily loss limit ───────────────────────────────────
        daily_loss_limit = self.daily_loss_limit
        if self.daily_pnl <= daily_loss_limit:
            return False, f"Daily loss limit hit: ₹{self.daily_pnl:,.2f} (limit: ₹{daily_loss_limit:,.2f})"

        # ── 3. Weekly loss limit ──────────────────────────────────
        weekly_loss_limit = self.weekly_loss_limit
        if self.weekly_pnl <= weekly_loss_limit:
            return False, f"Weekly loss limit hit: ₹{self.weekly_pnl:,.2f} (limit: ₹{weekly_loss_limit:,.2f})"

        # ── 4. Max trades per day ─────────────────────────────────
        if self.trades_today >= self.max_trades_per_day:
            return False, f"Max trades per day reached: {self.trades_today}/{self.max_trades_per_day}"

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

    @staticmethod
    def lot_size_for(symbol: str) -> int:
        """Smallest tradable quantity step of ``symbol``."""
        if symbol.endswith("-EQ"):
            return 1
        return max(1, int(settings.LOT_SIZE))

    def calculate_position_size(self, signal: Signal, current_price: float, lot_size: int = 1) -> int:
        """Risk-based position sizing, in whole lots of ``lot_size``.

        Returns 0 when even one lot would risk more than the allowed amount:
        the trade is skipped rather than taken over-sized.
        """
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

        lot_size = max(1, int(lot_size))
        raw_qty = int(adjusted_risk / risk_per_unit)
        lots = raw_qty // lot_size

        return lots * lot_size

    def update_after_trade(self, trade: Trade, now: datetime = None):
        """Update risk counters after trade close (the caller saves).

        ``now`` is IST wall-clock time (default: the current IST time).
        """
        if now is None:
            now = ist_now()
        self.roll_over(now)
        self.trades_today += 1
        self.daily_pnl += trade.realized_pnl
        self.weekly_pnl += trade.realized_pnl

        if trade.result and trade.result.value == "LOSS":
            self.consecutive_losses += 1
            self.last_loss_time = now
            if self.consecutive_losses >= 2:
                self.cooldown_until = now + timedelta(minutes=settings.COOLDOWN_MINUTES)
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
