"""Every closed decision candle records exactly one decision, with its reason."""
from datetime import datetime

import pytest

from bot import NiftyBot
from config import settings
from database import get_recent_decisions, init_db, save_decision
from models.candle import Candle
from models.enums import Direction
from models.signal import Signal

CANDLE_TIME = datetime(2026, 10, 26, 4, 30)  # 10:00 IST, naive UTC


def candle() -> Candle:
    return Candle(
        timestamp=CANDLE_TIME, open=24000, high=24010, low=23990, close=24005,
        volume=0, ema_20=23990, ema_50=23980, ema_200=23900, vwap=23950,
        rsi=55, macd_hist=1.5, atr=10,
    )


def signal(direction=Direction.LONG, score=80, rr=2.0, reasoning="test") -> Signal:
    return Signal(
        timestamp=datetime.now(), direction=direction, score=score, confidence=score / 100,
        reasoning=reasoning, stop_loss=23980, target_1=24025, target_2=24035,
        target_3=24045, risk_reward=rr,
    )


@pytest.fixture()
async def bot(db_path):
    await init_db()
    engine = NiftyBot()
    engine.is_running = True
    engine.broadcasts = []

    async def capture(data):
        engine.broadcasts.append(data)

    async def allow(*_args, **_kwargs):
        return True, "OK"

    async def analysis(c):
        return {"candle": c, "patterns": [], "divergence": None, "fvg": None,
                "liquidity_sweep": None, "higher_timeframe_trend": Direction.WAIT}

    async def no_structure(*_args, **_kwargs):
        return {"error": "offline"}

    engine._broadcast = capture
    engine.risk_manager.can_trade = allow
    engine.analyzer.analyze = analysis
    engine.get_structure = no_structure
    yield engine
    engine.is_running = False


def decisions_broadcast(engine):
    return [m["decision"] for m in engine.broadcasts if m["type"] == "decision"]


async def test_a_blocked_cycle_records_the_gate_reason(bot):
    async def blocked(*_args, **_kwargs):
        return False, "News blackout: RBI hikes repo rate..."

    bot.risk_manager.can_trade = blocked
    await bot._on_candle_close(candle())

    [d] = await get_recent_decisions()
    assert d["outcome"] == "BLOCKED"
    assert d["reason"] == "News blackout: RBI hikes repo rate..."
    assert d["direction"] is None and d["score"] is None
    assert d["candle_time"] == CANDLE_TIME.isoformat()
    assert d["symbol"] == settings.TRADING_SYMBOL and d["timeframe"] == "15m"
    assert d["price"] == 24005
    assert decisions_broadcast(bot) == [d]
    # The existing message is still sent.
    assert any(m["type"] == "skip" for m in bot.broadcasts)


async def test_no_setup_records_both_scores(bot):
    # The real engine: this candle scores LONG 60 (trend 25, VWAP 10, RSI 15,
    # MACD 10) and SHORT 8 (RSI 55 is in its 50-60 band); a threshold of 70
    # makes it a WAIT.
    bot.decision_engine.min_threshold = 70
    await bot._on_candle_close(candle())

    [d] = await get_recent_decisions()
    assert d["outcome"] == "WAIT"
    assert d["reason"] == "Score below threshold"
    assert (d["direction"], d["score"]) == ("WAIT", 0)
    assert (d["long_score"], d["short_score"]) == (60, 8)


async def test_a_rejected_signal_records_the_validation_reason(bot):
    bot.decision_engine.evaluate = lambda _a: signal(rr=1.0)
    await bot._on_candle_close(candle())

    [d] = await get_recent_decisions()
    assert d["outcome"] == "REJECTED"
    assert d["reason"] == f"R:R 1.00 below minimum {settings.MIN_RISK_REWARD}"
    assert (d["direction"], d["score"]) == ("LONG", 80)


async def test_a_signal_that_could_not_be_entered(bot):
    bot.decision_engine.evaluate = lambda _a: signal()

    async def size_zero(*_args):
        return "Position size 0: one lot would risk more than the per-trade limit"

    bot._enter_trade = size_zero
    await bot._on_candle_close(candle())

    [d] = await get_recent_decisions()
    assert d["outcome"] == "NOT_ENTERED"
    assert d["reason"].startswith("Position size 0")


async def test_an_entered_trade(bot):
    bot.decision_engine.evaluate = lambda _a: signal(direction=Direction.SHORT)

    async def entered(*_args):
        return None

    bot._enter_trade = entered
    await bot._on_candle_close(candle())

    [d] = await get_recent_decisions()
    assert d["outcome"] == "ENTERED"
    assert d["reason"] == "SHORT trade opened"
    assert d["direction"] == "SHORT"
    # Entered cycles never send the "wait" message.
    assert not any(m["type"] == "wait" for m in bot.broadcasts)


async def test_unknown_outcomes_are_refused(db_path):
    await init_db()
    with pytest.raises(ValueError):
        await save_decision({"candle_time": "x", "recorded_at": "x", "symbol": "S",
                             "timeframe": "15m", "outcome": "MAYBE"})


def test_api_lists_decisions_newest_first(client):
    import asyncio

    async def add(minute, outcome):
        await save_decision({
            "candle_time": datetime(2026, 10, 26, 4, minute).isoformat(),
            "recorded_at": datetime(2026, 10, 26, 4, minute).isoformat(),
            "symbol": "NSE:NIFTY50-INDEX", "timeframe": "15m",
            "outcome": outcome, "reason": outcome.lower(),
        })

    for minute, outcome in ((0, "BLOCKED"), (15, "WAIT"), (30, "REJECTED")):
        asyncio.run(add(minute, outcome))

    body = client.get("/api/decisions").json()
    assert [d["outcome"] for d in body["decisions"]] == ["REJECTED", "WAIT", "BLOCKED"]
    assert [d["outcome"] for d in client.get("/api/decisions?limit=1").json()["decisions"]] == ["REJECTED"]
    # Out-of-range limits are clamped, not refused.
    assert len(client.get("/api/decisions?limit=0").json()["decisions"]) == 1
    assert len(client.get("/api/decisions?limit=999").json()["decisions"]) == 3
