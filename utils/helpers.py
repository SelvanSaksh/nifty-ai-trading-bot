"""
Utility helpers for time handling, math operations, and data validation.
"""
from datetime import datetime, time, timedelta
from typing import List, Tuple, Union


# ── Timeframe helpers ────────────────────────────────────────────

# Canonical labels used across the API, the analyzer and the database.
TIMEFRAME_ALIASES = {
    "1": "1m", "1m": "1m",
    "5": "5m", "5m": "5m",
    "15": "15m", "15m": "15m",
    "30": "30m", "30m": "30m",
    "60": "1h", "60m": "1h", "1h": "1h",
}

# Broker (Fyers) resolution codes for each canonical timeframe.
RESOLUTION_BY_TIMEFRAME = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "1h": "60"}

TIMEFRAME_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60}

# Timeframes that are built live from ticks; everything else is history-only.
LIVE_TIMEFRAMES = ("5m", "15m", "1h")

SUPPORTED_TIMEFRAMES = tuple(TIMEFRAME_MINUTES)


def normalize_timeframe(value: str) -> str:
    """
    Normalize a user supplied timeframe to a canonical label.

    Raises ValueError for anything unsupported — a request must never silently
    fall back to a different timeframe, or the chart would show candles for a
    period the user did not ask for.
    """
    key = str(value or "").strip().lower()
    normalized = TIMEFRAME_ALIASES.get(key)
    if normalized is None:
        raise ValueError(
            f"Unsupported timeframe '{value}'. Supported: {', '.join(SUPPORTED_TIMEFRAMES)}"
        )
    return normalized


def timeframe_label(minutes: int) -> str:
    """Canonical label for a timeframe given in minutes."""
    return "1h" if minutes == 60 else f"{int(minutes)}m"


def resolution_for(timeframe: str) -> str:
    """Broker resolution code for a canonical (or alias) timeframe."""
    return RESOLUTION_BY_TIMEFRAME[normalize_timeframe(timeframe)]


def align_timestamp(ts: datetime, timeframe: str) -> datetime:
    """Floor a timestamp to the opening boundary of its timeframe bar."""
    minutes = TIMEFRAME_MINUTES[normalize_timeframe(timeframe)]
    return ts.replace(
        minute=(ts.minute // minutes) * minutes, second=0, microsecond=0
    )


def is_market_open(now: datetime = None) -> bool:
    """Check if Indian markets are currently open."""
    if now is None:
        now = datetime.now()
    
    # Weekends closed
    if now.weekday() >= 5:
        return False
    
    # Market hours: 9:15 AM to 3:30 PM IST
    market_start = time(9, 15)
    market_end = time(15, 30)
    
    return market_start <= now.time() <= market_end


def is_pre_market(now: datetime = None) -> bool:
    """Check if in pre-market session (9:00 - 9:15)."""
    if now is None:
        now = datetime.now()
    
    pre_start = time(9, 0)
    pre_end = time(9, 15)
    
    return pre_start <= now.time() < pre_end and now.weekday() < 5


def round_to_tick(price: float, tick_size: float = 0.05) -> float:
    """Round price to nearest tick size (Nifty tick = 0.05)."""
    return round(price / tick_size) * tick_size


def round_to_lot(qty: int, lot_size: int = 50) -> int:
    """Round quantity to nearest lot size."""
    return (qty // lot_size) * lot_size


def calculate_rr(entry: float, stop: float, target: float, direction: str) -> float:
    """Calculate risk-reward ratio."""
    risk = abs(entry - stop)
    reward = abs(target - entry)
    
    if risk <= 0:
        return 0.0
    
    return reward / risk


def pct_change(current: float, previous: float) -> float:
    """Calculate percentage change."""
    if previous == 0:
        return 0.0
    return ((current - previous) / previous) * 100


def safe_divide(a: float, b: float, default: float = 0.0) -> float:
    """Safe division with default value."""
    return a / b if b != 0 else default


def normalize(value: float, min_val: float, max_val: float) -> float:
    """Normalize value to 0-1 range."""
    if max_val == min_val:
        return 0.0
    return (value - min_val) / (max_val - min_val)


def clamp(value: float, min_val: float, max_val: float) -> float:
    """Clamp value between min and max."""
    return max(min_val, min(max_val, value))


def slope(values: List[float], period: int = 5) -> float:
    """Calculate slope/angle of recent values."""
    if len(values) < period:
        return 0.0
    
    recent = values[-period:]
    x = list(range(period))
    
    n = period
    sum_x = sum(x)
    sum_y = sum(recent)
    sum_xy = sum(xi * yi for xi, yi in zip(x, recent))
    sum_x2 = sum(xi ** 2 for xi in x)
    
    denominator = n * sum_x2 - sum_x ** 2
    if denominator == 0:
        return 0.0
    
    return (n * sum_xy - sum_x * sum_y) / denominator


def is_bullish_candle(open_p: float, close: float) -> bool:
    """Check if candle is bullish (close > open)."""
    return close > open_p


def is_bearish_candle(open_p: float, close: float) -> bool:
    """Check if candle is bearish (close < open)."""
    return close < open_p


def candle_body(open_p: float, close: float) -> float:
    """Get candle body size."""
    return abs(close - open_p)


def candle_range(high: float, low: float) -> float:
    """Get full candle range (high - low)."""
    return high - low


def upper_wick(high: float, open_p: float, close: float) -> float:
    """Get upper wick size."""
    body_top = max(open_p, close)
    return high - body_top


def lower_wick(low: float, open_p: float, close: float) -> float:
    """Get lower wick size."""
    body_bottom = min(open_p, close)
    return body_bottom - low


def is_doji(open_p: float, high: float, low: float, close: float, threshold: float = 0.05) -> bool:
    """Check if candle is a doji (body very small relative to range)."""
    body = candle_body(open_p, close)
    range_ = candle_range(high, low)
    
    if range_ == 0:
        return False
    
    return body / range_ < threshold


def time_to_next_candle(now: datetime = None, timeframe_minutes: int = 15) -> int:
    """Get seconds until next candle closes."""
    if now is None:
        now = datetime.now()
    
    minutes = now.minute
    next_candle_minute = ((minutes // timeframe_minutes) + 1) * timeframe_minutes
    
    if next_candle_minute >= 60:
        next_time = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    else:
        next_time = now.replace(minute=next_candle_minute, second=0, microsecond=0)
    
    return int((next_time - now).total_seconds())


def format_pnl(pnl: float) -> str:
    """Format P&L with emoji."""
    if pnl > 0:
        return f"✅ +₹{pnl:,.2f}"
    elif pnl < 0:
        return f"❌ -₹{abs(pnl):,.2f}"
    return "➖ ₹0.00"


def format_number(num: Union[int, float], decimals: int = 2) -> str:
    """Format number with Indian comma separators."""
    if isinstance(num, int):
        return f"{num:,}"
    return f"{num:,.{decimals}f}"


def truncate_string(s: str, max_length: int = 100, suffix: str = "...") -> str:
    """Truncate string with ellipsis."""
    if len(s) <= max_length:
        return s
    return s[:max_length - len(suffix)] + suffix


def chunk_list(lst: List, chunk_size: int) -> List[List]:
    """Split list into chunks."""
    return [lst[i:i + chunk_size] for i in range(0, len(lst), chunk_size)]


def deduplicate_preserving_order(seq: List) -> List:
    """Remove duplicates while preserving order."""
    seen = set()
    result = []
    for item in seq:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def find_nearest(values: List[float], target: float) -> Tuple[float, int]:
    """Find nearest value and its index."""
    if not values:
        return 0.0, -1
    
    diffs = [abs(v - target) for v in values]
    min_idx = diffs.index(min(diffs))
    return values[min_idx], min_idx