"""
IST wall-clock helpers.

Candles and trade times are stored as naive UTC (the API contract), but the
trading rules - trading hours, the square-off time, which day a loss counts
towards - are Indian Standard Time. The server's own clock zone (UTC inside
Docker) must never decide them, so every rule converts explicitly.

IST is a fixed UTC+05:30 with no daylight saving, so no tz database is needed
(Windows and slim Docker images often lack one).
"""
from datetime import datetime, time, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30), "IST")


def to_ist(utc_naive: datetime) -> datetime:
    """Naive UTC -> naive IST wall-clock time."""
    return utc_naive.replace(tzinfo=timezone.utc).astimezone(IST).replace(tzinfo=None)


def ist_now() -> datetime:
    """Current IST wall-clock time (naive)."""
    return to_ist(datetime.utcnow())


def parse_hhmm(text: str) -> time:
    """'09:15' -> time(9, 15)."""
    return datetime.strptime(text.strip(), "%H:%M").time()
