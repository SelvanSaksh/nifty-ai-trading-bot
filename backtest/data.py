"""
1-minute history for backtests: fetched from Fyers once, cached as CSV.

Times are naive UTC, like every candle in the app.
"""
import csv
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from models.candle import Candle
from utils.live_bars import in_session

CACHE_DIR = Path("data") / "backtest"
FIELDS = ("timestamp", "open", "high", "low", "close", "volume")


def cache_path(symbol: str, cache_dir: Path = CACHE_DIR) -> Path:
    safe = symbol.replace(":", "_").replace("/", "_")
    return cache_dir / f"{safe}_1m.csv"


def save_csv(path: Path, candles: List[Candle]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(FIELDS)
        for c in candles:
            writer.writerow([c.timestamp.isoformat(), c.open, c.high, c.low, c.close, c.volume])


def load_csv(path: Path, start: Optional[datetime] = None, end: Optional[datetime] = None) -> List[Candle]:
    """Session 1m candles from ``path`` (optionally within [start, end]), oldest first."""
    candles = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            ts = datetime.fromisoformat(row["timestamp"])
            if (start and ts < start) or (end and ts > end) or not in_session(ts):
                continue
            candles.append(Candle(
                timestamp=ts,
                open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]),
                volume=int(float(row["volume"] or 0)), timeframe="1m",
            ))
    candles.sort(key=lambda c: c.timestamp)
    return candles


async def fetch(symbol: str, start: datetime, end: datetime, cache_dir: Path = CACHE_DIR) -> Path:
    """Download every 1m candle of ``symbol`` in [start, end] from Fyers and
    merge it into the cache. Needs a valid Fyers login."""
    from brokers.fyers_broker import FyersBroker

    broker = FyersBroker()
    if not await broker.connect():
        raise ConnectionError("Fyers login needed: open /api/auth/login, then retry")
    try:
        fresh = await broker.get_history_range(symbol, "1", start, end)
    finally:
        await broker.disconnect()

    path = cache_path(symbol, cache_dir)
    by_time = {c.timestamp: c for c in (load_csv(path) if path.exists() else [])}
    for c in fresh:
        by_time[c.timestamp] = c
    save_csv(path, [by_time[t] for t in sorted(by_time)])
    return path


def synthetic(days: int, start: datetime = datetime(2026, 1, 5), seed: int = 7,
              base: float = 24000.0, volume: int = 0) -> List[Candle]:
    """Deterministic random-walk session candles (tests and demos only)."""
    import random

    rng = random.Random(seed)
    candles: List[Candle] = []
    price = base
    day = start
    made = 0
    while made < days:
        if day.weekday() < 5:
            t = day.replace(hour=3, minute=45)
            close_at = day.replace(hour=10, minute=0)
            drift = rng.uniform(-0.0004, 0.0004)
            while t < close_at:
                o = price
                c = o * (1 + drift + rng.gauss(0, 0.0006))
                h = max(o, c) * (1 + abs(rng.gauss(0, 0.0003)))
                lo = min(o, c) * (1 - abs(rng.gauss(0, 0.0003)))
                candles.append(Candle(
                    timestamp=t, open=round(o, 2), high=round(h, 2), low=round(lo, 2),
                    close=round(c, 2), volume=volume, timeframe="1m",
                ))
                price = c
                t += timedelta(minutes=1)
            made += 1
        day += timedelta(days=1)
    return candles
