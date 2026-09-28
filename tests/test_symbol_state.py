"""Shared active-symbol state: persistence, versioning, audit, reconciliation."""
from datetime import datetime

import pytest

from bot import NiftyBot, SymbolConflictError, SymbolValidationError
from config import settings
from database import (
    get_active_symbol_state,
    get_recent_signals,
    get_symbol_change_history,
    init_db,
    save_signal,
    set_active_symbol_state,
)
from models.enums import Direction
from models.signal import Signal
from models.trade import Trade


def _open_trade(symbol: str = "NSE:NIFTY50-INDEX") -> Trade:
    return Trade(
        entry_time=datetime.now(),
        direction=Direction.LONG,
        entry_price=24500.0,
        quantity=50,
        initial_qty=50,
        remaining_qty=50,
        stop_loss=24400.0,
        target_1=24600.0,
        target_2=24700.0,
        target_3=24800.0,
        signal_score=70,
        confidence=0.7,
        symbol=symbol,
    )


def _make_signal() -> Signal:
    return Signal(
        timestamp=datetime.now(),
        direction=Direction.LONG,
        score=72,
        confidence=0.72,
        reasoning="test",
        stop_loss=100.0,
        target_1=110.0,
        target_2=120.0,
        target_3=130.0,
        risk_reward=2.0,
    )


# ── Database level ───────────────────────────────────────────────

async def test_active_symbol_defaults_to_config(db_path):
    await init_db()
    state = await get_active_symbol_state()
    assert state["symbol"] == settings.TRADING_SYMBOL
    assert state["symbol_name"] == settings.TRADING_SYMBOL_NAME
    assert state["version"] == 0
    assert state["changed_at"] is None
    assert state["changed_by"] == "default"


async def test_set_state_is_versioned_and_audited(db_path):
    await init_db()

    first = await set_active_symbol_state(
        "NSE:INFY-EQ", "Infosys", updated_by="unit-test", reason="first"
    )
    assert first["version"] == 1

    second = await set_active_symbol_state(
        settings.TRADING_SYMBOL,
        settings.TRADING_SYMBOL_NAME,
        updated_by="other-worker",
        reason="second",
    )
    assert second["version"] == 2

    current = await get_active_symbol_state()
    assert current["symbol"] == settings.TRADING_SYMBOL
    assert current["version"] == 2
    assert current["changed_by"] == "other-worker"

    history = await get_symbol_change_history(10)
    assert [h["to_symbol"] for h in history] == [settings.TRADING_SYMBOL, "NSE:INFY-EQ"]
    assert history[0]["from_symbol"] == "NSE:INFY-EQ"
    assert history[0]["reason"] == "second"


async def test_signals_keep_their_symbol(db_path):
    """History must stay readable when the bot switches instruments."""
    await init_db()
    signal = _make_signal()
    await save_signal(signal, "NSE:INFY-EQ", "Infosys")

    stored = await get_recent_signals(5)
    assert len(stored) == 1
    assert stored[0].symbol == "NSE:INFY-EQ"
    assert stored[0].symbol_name == "Infosys"


# ── Engine level ─────────────────────────────────────────────────

async def test_set_symbol_applies_and_persists(db_path):
    await init_db()
    bot = NiftyBot()

    result = await bot.set_symbol("NSE:INFY-EQ", changed_by="unit-test")
    assert result["symbol"] == "NSE:INFY-EQ"
    assert result["version"] == 1
    assert result["changed_by"] == "unit-test"

    assert bot.symbol == "NSE:INFY-EQ"
    assert bot.symbol_version == 1
    # Candle buffers now belong to Infosys — no leftover index data.
    assert bot.analyzer_symbol == "NSE:INFY-EQ"
    assert bot.analyzer.candles_15m
    assert all(c.close < 5000 for c in bot.analyzer.candles_15m)

    persisted = await get_active_symbol_state()
    assert persisted["symbol"] == "NSE:INFY-EQ"


async def test_set_symbol_noop_when_already_active(db_path):
    await init_db()
    bot = NiftyBot()

    await bot.set_symbol("NSE:INFY-EQ", "Infosys")
    again = await bot.set_symbol("NSE:INFY-EQ", "Infosys")

    assert again["message"] == "Symbol already active"
    assert again["version"] == 1  # version did not churn
    assert len(await get_symbol_change_history(10)) == 1


async def test_set_symbol_rejects_open_trade(db_path):
    await init_db()
    bot = NiftyBot()
    bot.active_trade = _open_trade()

    with pytest.raises(SymbolConflictError):
        await bot.set_symbol("NSE:INFY-EQ")

    assert bot.symbol == settings.TRADING_SYMBOL
    assert (await get_active_symbol_state())["symbol"] == settings.TRADING_SYMBOL


async def test_set_symbol_rejects_bad_input(db_path):
    await init_db()
    bot = NiftyBot()

    with pytest.raises(SymbolValidationError):
        await bot.set_symbol("garbage")
    with pytest.raises(SymbolValidationError):
        await bot.set_symbol("NSE:NOT-A-REAL-CODE")
    with pytest.raises(SymbolValidationError):
        await bot.set_symbol("")

    assert bot.symbol == settings.TRADING_SYMBOL
    assert (await get_active_symbol_state())["version"] == 0


async def test_reconcile_applies_change_written_by_another_process(db_path):
    """A write from another worker must converge into this engine."""
    await init_db()
    bot = NiftyBot()
    bot.is_running = True  # simulate the live engine without spawning ticks
    try:
        external = await set_active_symbol_state(
            "NSE:INFY-EQ", "Infosys", updated_by="other-worker"
        )
        assert bot.symbol != "NSE:INFY-EQ"

        assert await bot._reconcile_once() is True
        assert bot.symbol == "NSE:INFY-EQ"
        assert bot.symbol_version == external["version"]
        assert bot.analyzer_symbol == "NSE:INFY-EQ"

        # Already in sync: nothing to do, no version churn.
        assert await bot._reconcile_once() is False
    finally:
        bot.is_running = False


async def test_reconcile_defers_switch_while_trade_is_open(db_path):
    await init_db()
    bot = NiftyBot()
    bot.is_running = True
    try:
        external = await set_active_symbol_state(
            "NSE:INFY-EQ", "Infosys", updated_by="other-worker"
        )
        bot.active_trade = _open_trade()

        assert await bot._reconcile_once() is False
        assert bot.symbol != "NSE:INFY-EQ"  # trade left untouched
        assert bot._pending_symbol_state is not None

        bot.active_trade = None
        await bot._apply_pending_symbol(external)

        assert bot.symbol == "NSE:INFY-EQ"
        assert bot.symbol_version == external["version"]
        assert bot._pending_symbol_state is None
    finally:
        bot.is_running = False


# ── Candle queries ───────────────────────────────────────────────

async def test_get_candles_envelope_names_the_instrument(db_path):
    await init_db()
    bot = NiftyBot()

    result = await bot.get_candles(timeframe="15m", limit=40)

    assert result["symbol"] == settings.TRADING_SYMBOL
    assert result["active_symbol"] == settings.TRADING_SYMBOL
    assert result["timeframe"] == "15m"
    assert result["count"] == 40
    assert result["error"] is None
    assert all(c["symbol"] == result["symbol"] for c in result["candles"])
    assert all(c["symbol_name"] == result["symbol_name"] for c in result["candles"])
    assert all(c["timeframe"] == "15m" for c in result["candles"])
    assert result["coverage"] is not None


async def test_get_candles_for_a_different_instrument(db_path):
    await init_db()
    bot = NiftyBot()

    result = await bot.get_candles(symbol="NSE:INFY-EQ", limit=25)

    assert result["symbol"] == "NSE:INFY-EQ"
    assert result["active_symbol"] == settings.TRADING_SYMBOL
    assert result["count"] == 25
    # Infosys prices, not Nifty index levels.
    assert all(c["close"] < 5000 for c in result["candles"])
    assert all(c["symbol"] == "NSE:INFY-EQ" for c in result["candles"])


async def test_get_candles_rejects_bad_input(db_path):
    await init_db()
    bot = NiftyBot()

    with pytest.raises(ValueError):
        await bot.get_candles(timeframe="7m")
    with pytest.raises(ValueError):
        await bot.get_candles(limit=0)
    with pytest.raises(ValueError):
        await bot.get_candles(limit=5000)
    with pytest.raises(ValueError):
        await bot.get_candles(end_time="yesterday")
    with pytest.raises(SymbolValidationError):
        await bot.get_candles(symbol="not-a-symbol")


async def test_get_symbol_reports_sync_status(db_path):
    await init_db()
    bot = NiftyBot()

    state = await bot.get_symbol()
    assert state["symbol"] == settings.TRADING_SYMBOL
    assert state["version"] == 0
    assert state["engine_leader"] is False
    assert state["bot_running"] is False
    assert state["in_sync"] is True
