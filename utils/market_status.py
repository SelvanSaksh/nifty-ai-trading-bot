"""Is the market open right now?

The schedule alone cannot say: the app has no exchange-holiday calendar, and
Fyers keeps sending index ticks after the close. So "open" needs both the
NSE session hours (Mon-Fri 09:15-15:30 IST) and a recent live tick whose
exchange time falls inside today's session. Session hours without such a tick
are reported as such ("no_ticks"): an exchange holiday, or a dead data feed.

Every time here is naive UTC, like every time the API serves.
"""
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from utils.live_bars import SESSION_CLOSE_MIN, SESSION_OPEN_MIN, in_session

#: Longest gap between live ticks that still counts as a live market.
LIVE_TICK_MAX_AGE = timedelta(seconds=120)


def _at_minute(day: datetime, minute: int) -> datetime:
    return day.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=minute)


def is_live_session_tick(exchange_time: datetime, received_at: datetime) -> bool:
    """A tick that proves the session is trading: its exchange time is inside
    today's session hours (not an after-hours print, not a stale one)."""
    return exchange_time.date() == received_at.date() and in_session(exchange_time)


def next_session_open(now: datetime) -> datetime:
    """The next scheduled session open after ``now`` (holidays unknown)."""
    open_today = _at_minute(now, SESSION_OPEN_MIN)
    if now.weekday() < 5 and now < open_today:
        return open_today
    day = now + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return _at_minute(day, SESSION_OPEN_MIN)


def market_status(now: datetime, last_live_tick: Optional[datetime]) -> Dict[str, Any]:
    """``state``: "open", "no_ticks" (session hours, no live tick) or "closed".

    ``reason`` says why it is not open: "no_live_ticks", "weekend",
    "before_open" or "after_close" (None when open).
    """
    last = last_live_tick.isoformat() if last_live_tick else None
    if in_session(now):
        live = (
            last_live_tick is not None
            and timedelta(0) <= now - last_live_tick <= LIVE_TICK_MAX_AGE
        )
        return {
            "state": "open" if live else "no_ticks",
            "reason": None if live else "no_live_ticks",
            "closes_at": _at_minute(now, SESSION_CLOSE_MIN).isoformat(),
            "next_open": None,
            "last_live_tick": last,
        }
    if now.weekday() >= 5:
        reason = "weekend"
    elif now.hour * 60 + now.minute < SESSION_OPEN_MIN:
        reason = "before_open"
    else:
        reason = "after_close"
    return {
        "state": "closed",
        "reason": reason,
        "closes_at": None,
        "next_open": next_session_open(now).isoformat(),
        "last_live_tick": last,
    }
