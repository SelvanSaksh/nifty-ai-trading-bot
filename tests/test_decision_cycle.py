"""Closed-candle decision cycle: awaited analysis, symbol-tagged records."""
import warnings

import aiosqlite

from bot import NiftyBot
from config import settings
from database import get_recent_signals, init_db


async def test_candle_close_records_the_symbol(db_path):
    await init_db()

    bot = NiftyBot()
    bot.is_running = True
    bot.analyzer.candles_15m = await bot._fetch_history(
        settings.TRADING_SYMBOL, "15m", 120, use_cache=False
    )
    bot.analyzer_symbol = settings.TRADING_SYMBOL

    async def _force_open(*args, **kwargs):
        return True, "OK"

    bot.risk_manager.can_trade = _force_open

    broadcasts = []

    async def _capture(data):
        broadcasts.append(data)

    bot._broadcast = _capture

    candle = bot.analyzer.candles_15m[-1]
    # Same order as production: indicators are computed when the bar closes.
    bot.analyzer._finalize_candle(candle)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await bot._on_candle_close(candle)

    never_awaited = [w for w in caught if "never awaited" in str(w.message)]
    assert not never_awaited, f"un-awaited coroutine in analysis: {never_awaited}"

    signals = await get_recent_signals(5)
    assert signals, "closing a candle must record a signal"
    assert signals[0].symbol == settings.TRADING_SYMBOL
    assert signals[0].symbol_name == settings.TRADING_SYMBOL_NAME

    assert {m["type"] for m in broadcasts} & {"signal", "wait"}

    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT symbol FROM candles ORDER BY id DESC LIMIT 1")
        row = await cursor.fetchone()
    assert row is not None
    assert row["symbol"] == settings.TRADING_SYMBOL

    bot.is_running = False
