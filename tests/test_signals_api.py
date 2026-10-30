"""Signals: naive-UTC timestamps, the evaluated price, LONG/SHORT-only reads,
and decision recording that can never break a decision cycle."""
import asyncio
from datetime import datetime, timedelta

import aiosqlite

from database import get_recent_signals, init_db, save_signal
from decision_engine import DecisionEngine
from models.candle import Candle
from models.enums import Direction
from models.signal import Signal


def analysis(close=100.0, rsi=55):
    candle = Candle(timestamp=datetime(2026, 10, 26, 4, 30), open=close, high=close + 1,
                    low=close - 1, close=close, volume=0, ema_20=99, ema_50=98,
                    ema_200=97, vwap=95, rsi=rsi, macd_hist=1.0, atr=1)
    return {"candle": candle, "patterns": [], "divergence": None, "fvg": None,
            "liquidity_sweep": None, "higher_timeframe_trend": Direction.WAIT}


def test_signals_are_stamped_in_utc_with_the_evaluated_price():
    engine = DecisionEngine()
    long_signal = engine.evaluate(analysis(close=24005.5))
    engine.min_threshold = 101
    wait_signal = engine.evaluate(analysis(close=24005.5))

    assert long_signal.direction == Direction.LONG
    assert wait_signal.direction == Direction.WAIT
    for signal in (long_signal, wait_signal):
        # Not the host's local time (this suite also runs on IST machines).
        assert abs(signal.timestamp - datetime.utcnow()) < timedelta(seconds=5)
        assert signal.price == 24005.5


def make(direction, minute, price=24000.0):
    return Signal(timestamp=datetime(2026, 10, 26, 4, minute), direction=direction,
                  score=0 if direction == Direction.WAIT else 70,
                  confidence=0.0 if direction == Direction.WAIT else 0.7,
                  reasoning="r", stop_loss=1, target_1=2, target_2=3, target_3=4,
                  risk_reward=2.0, price=price)


async def test_price_round_trips_and_waits_can_be_left_out(db_path):
    await init_db()
    await save_signal(make(Direction.LONG, 0, 24001.0), "NSE:NIFTY50-INDEX")
    await save_signal(make(Direction.WAIT, 15), "NSE:NIFTY50-INDEX")
    await save_signal(make(Direction.SHORT, 30, 24003.0), "NSE:NIFTY50-INDEX")

    every = await get_recent_signals(10)
    assert [s.direction for s in every] == [Direction.SHORT, Direction.WAIT, Direction.LONG]
    assert every[0].price == 24003.0

    actionable = await get_recent_signals(10, include_wait=False)
    assert [s.direction for s in actionable] == [Direction.SHORT, Direction.LONG]
    assert [s.price for s in actionable] == [24003.0, 24001.0]


async def test_signals_saved_before_the_price_column_read_as_none(db_path):
    await init_db()
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            "INSERT INTO signals (timestamp, direction, score, confidence, reasoning, "
            "stop_loss, target_1, target_2, target_3, risk_reward, ht_filter_passed) "
            "VALUES ('2026-10-26T04:00:00', 'LONG', 70, 0.7, 'old', 1, 2, 3, 4, 2.0, 1)"
        )
        await db.commit()
    [old] = await get_recent_signals(5)
    assert old.price is None


def test_api_filters_waits_and_clamps_the_limit(client):
    for direction, minute in ((Direction.LONG, 0), (Direction.WAIT, 15), (Direction.WAIT, 30)):
        asyncio.run(save_signal(make(direction, minute), "NSE:NIFTY50-INDEX"))

    assert len(client.get("/api/signals").json()) == 3
    actionable = client.get("/api/signals?include_wait=false").json()
    assert [s["direction"] for s in actionable] == ["LONG"]
    assert actionable[0]["price"] == 24000.0
    assert len(client.get("/api/signals?limit=0").json()) == 1
    assert len(client.get("/api/signals?limit=9999").json()) == 3


async def test_a_failed_decision_record_does_not_break_the_cycle(db_path, monkeypatch):
    from bot import NiftyBot

    await init_db()
    bot = NiftyBot()
    sent = []

    async def capture(data):
        sent.append(data)

    async def blocked(*_args, **_kwargs):
        return False, "Outside trading hours (09:15-15:15 IST)"

    async def broken(_decision):
        raise RuntimeError("database is locked")

    bot._broadcast = capture
    bot.risk_manager.can_trade = blocked
    monkeypatch.setattr("bot.save_decision", broken)

    await bot._on_candle_close(analysis()["candle"])  # must not raise

    assert [m["type"] for m in sent] == ["skip"]
