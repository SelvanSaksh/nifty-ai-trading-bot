"""
Exit rules of an open trade: square-off, stop-loss and the three targets.

Pure functions, shared by the live engine (bot.py) and the backtester, so a
backtest manages trades exactly as the bot does.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from config import settings
from models.enums import Direction, TradeStatus
from models.trade import Trade
from utils.market_time import parse_hhmm, to_ist

#: Share of the *initial* quantity taken off at T1 and T2. The rest rides to T3.
T1_FRACTION = 0.5
T2_FRACTION = 0.375


@dataclass(frozen=True)
class ExitAction:
    reason: str                    # EOD_SQUARE_OFF, STOP_LOSS, TARGET_1, TARGET_2, TARGET_3
    price: float                   # price a paper fill is booked at
    fraction: Optional[float]      # partial exit (share of initial qty); None = all
    new_status: Optional[TradeStatus] = None  # status after a partial exit


def square_off_due(trade: Trade, now_utc: datetime) -> bool:
    """At/after the square-off time, or the trade is from an earlier day."""
    now = to_ist(now_utc)
    return (
        now.time() >= parse_hhmm(settings.SQUARE_OFF_TIME)
        or to_ist(trade.entry_time).date() < now.date()
    )


def _reached(trade: Trade, price: float, level: float) -> bool:
    if trade.direction == Direction.LONG:
        return price >= level
    return price <= level


def next_exit(trade: Trade, price: float, now_utc: datetime) -> Optional[ExitAction]:
    """What the trade must do at ``price`` (a tick at ``now_utc``), if anything."""
    if square_off_due(trade, now_utc):
        return ExitAction("EOD_SQUARE_OFF", price, None)

    # Stop loss first (highest priority). Booked at the price that crossed
    # it: a stop is a market exit once triggered, and gaps fill beyond it.
    stop_hit = (
        (trade.direction == Direction.LONG and price <= trade.stop_loss)
        or (trade.direction == Direction.SHORT and price >= trade.stop_loss)
    )
    if stop_hit:
        return ExitAction("STOP_LOSS", price, None)

    if trade.status == TradeStatus.OPEN and _reached(trade, price, trade.target_1):
        return ExitAction("TARGET_1", trade.target_1, T1_FRACTION, TradeStatus.PARTIAL_1)
    if trade.status == TradeStatus.PARTIAL_1 and _reached(trade, price, trade.target_2):
        return ExitAction("TARGET_2", trade.target_2, T2_FRACTION, TradeStatus.PARTIAL_2)
    if trade.status == TradeStatus.PARTIAL_2 and _reached(trade, price, trade.target_3):
        return ExitAction("TARGET_3", trade.target_3, None)
    return None


def partial_qty(trade: Trade, fraction: float, lot: int) -> int:
    """Whole lots of ``fraction`` of the initial quantity (0 = too small to split)."""
    qty = int(trade.initial_qty * fraction) // lot * lot
    return min(qty, trade.remaining_qty)
