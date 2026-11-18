"""Manual exit of the open trade (POST /api/trades/exit) and the live day
P&L (``day_pnl`` in /api/status, ``pnl`` messages on /ws/live)."""
import json
from datetime import timedelta

import pytest

from bot import TradeExitError
from database import get_open_trade, get_recent_trades
from models.enums import Direction, SignalResult, TradeStatus
from risk_manager import RiskManager
from tests.test_trading_safety import (  # noqa: F401  (fixtures)
    EQ,
    MONDAY_10_IST,
    T0,
    enter,
    live_bot,
    make_signal,
    paper_bot,
)


class Client:
    """A /ws/live client, as Starlette serialises to it."""

    def __init__(self):
        self.sent = []

    async def send_json(self, data):
        self.sent.append(json.loads(json.dumps(data)))

    def of_type(self, kind):
        return [m for m in self.sent if m["type"] == kind]


# ── Manual exit: paper ────────────────────────────────────────────

async def test_a_paper_trade_exits_at_the_last_price(paper_bot):
    client = Client()
    paper_bot.add_websocket_client(client)
    await enter(paper_bot)
    trade = paper_bot.active_trade
    paper_bot._last_price = 806.5

    result = await paper_bot.exit_trade_now(trade.id)

    assert paper_bot.active_trade is None
    assert (trade.status, trade.exit_reason, trade.exit_price) == (
        TradeStatus.CLOSED, "MANUAL_EXIT", 806.5,
    )
    assert trade.realized_pnl == 650.0  # (806.5 - 800) x 100
    assert trade.result == SignalResult.WIN
    # Booked like any close: the day's P&L and trade count.
    assert paper_bot.today_pnl == 650.0
    assert paper_bot.risk_manager.trades_today == 1
    assert await get_open_trade() is None
    saved = (await get_recent_trades(1))[0]
    assert (saved.exit_reason, saved.realized_pnl) == ("MANUAL_EXIT", 650.0)

    assert result["trade"]["id"] == trade.id
    assert result["message"] == f"Trade {trade.id} closed at 806.5: P&L +650.00"
    assert result["day_pnl"] == {
        "closed": 650.0, "open_realized": 0.0, "unrealized": None,
        "total": 650.0, "marked": True, "position": None,
    }
    # The dashboard hears of it at once: the close, then the flat P&L.
    assert client.of_type("trade_closed")[-1]["trade"]["exit_reason"] == "MANUAL_EXIT"
    assert client.sent[-1]["type"] == "pnl"
    assert client.sent[-1]["total"] == 650.0 and client.sent[-1]["position"] is None


async def test_a_paper_short_exit_books_a_loss(paper_bot):
    await enter(paper_bot, make_signal(direction=Direction.SHORT, stop=810.0, t1=780.0, t2=770.0, t3=760.0))
    trade = paper_bot.active_trade
    paper_bot._last_price = 803.0
    await paper_bot.exit_trade_now()
    assert trade.realized_pnl == -300.0  # short 100 from 800, out at 803
    assert trade.result == SignalResult.LOSS
    assert paper_bot.risk_manager.consecutive_losses == 1


async def test_without_an_open_trade_nothing_happens(paper_bot):
    with pytest.raises(TradeExitError) as err:
        await paper_bot.exit_trade_now()
    assert (str(err.value), err.value.status) == ("No trade is open", 409)


async def test_another_trade_id_is_refused(paper_bot):
    await enter(paper_bot)
    paper_bot._last_price = 805.0
    with pytest.raises(TradeExitError) as err:
        await paper_bot.exit_trade_now("TRD_OLD")
    assert err.value.status == 409 and "is not the open trade" in str(err.value)
    assert paper_bot.active_trade is not None


async def test_a_paper_exit_needs_a_price(paper_bot):
    await enter(paper_bot)
    paper_bot._last_price = None  # restarted, no tick yet
    with pytest.raises(TradeExitError) as err:
        await paper_bot.exit_trade_now()
    assert err.value.status == 409 and "No price for" in str(err.value)
    assert paper_bot.active_trade.status == TradeStatus.OPEN


async def test_a_stopped_bot_cannot_exit(paper_bot):
    await enter(paper_bot)
    paper_bot._last_price = 805.0
    paper_bot.is_running = False
    with pytest.raises(TradeExitError) as err:
        await paper_bot.exit_trade_now()
    assert err.value.status == 409 and "The bot is stopped" in str(err.value)
    assert paper_bot.active_trade is not None


async def test_a_frozen_trade_is_not_touched(live_bot):
    await enter(live_bot)
    live_bot._trade_frozen = True
    live_bot.broker.calls.clear()
    with pytest.raises(TradeExitError) as err:
        await live_bot.exit_trade_now()
    assert err.value.status == 409 and "unknown" in str(err.value)
    assert live_bot.broker.calls == []


async def test_an_exit_after_t1_books_the_rest(paper_bot):
    paper_bot.symbol = "NSE:NIFTY50-INDEX"
    await enter(paper_bot, make_signal(stop=24495.0, t1=24510, t2=24520, t3=24530), price=24500.0)
    trade = paper_bot.active_trade
    await paper_bot._manage_trade(24510.0, T0 + timedelta(minutes=5))  # T1: 100 of 200 out
    paper_bot._last_price = 24512.0
    await paper_bot.exit_trade_now()
    # 100 x 10 at T1, then 100 x 12 now.
    assert trade.realized_pnl == 2200.0
    assert paper_bot.today_pnl == 2200.0


# ── Manual exit: live ─────────────────────────────────────────────

async def test_a_live_exit_cancels_the_stop_then_sells_at_market(live_bot):
    await enter(live_bot)
    trade = live_bot.active_trade
    stop_id = trade.sl_order_id
    live_bot._last_price = 804.0
    live_bot.broker._symbol_prices[EQ] = 804.5  # the broker's fill
    live_bot.broker.calls.clear()

    await live_bot.exit_trade_now(trade.id)

    assert live_bot.broker.calls == [("cancel", stop_id), ("market", -1, 100)]
    assert live_bot.active_trade is None
    assert (trade.exit_reason, trade.exit_price, trade.realized_pnl) == ("MANUAL_EXIT", 804.5, 450.0)


async def test_a_live_exit_that_does_not_fill_reports_it(live_bot):
    await enter(live_bot)
    trade = live_bot.active_trade
    live_bot._last_price = 804.0
    live_bot.broker.market_fills = False
    with pytest.raises(TradeExitError) as err:
        await live_bot.exit_trade_now()
    assert err.value.status == 502 and "did not complete: 100" in str(err.value)
    # Still open and protected again at the broker.
    assert live_bot.active_trade is trade and trade.remaining_qty == 100
    assert trade.sl_order_id is not None


async def test_a_live_exit_after_the_stop_fired_books_the_stop(live_bot):
    await enter(live_bot)
    trade = live_bot.active_trade
    live_bot.broker.fire_stop(trade.sl_order_id, 789.0)
    live_bot._last_price = 789.0
    result = await live_bot.exit_trade_now()
    assert (trade.exit_reason, trade.exit_price) == ("STOP_LOSS", 789.0)
    assert result["message"].startswith(f"Trade {trade.id} closed by STOP_LOSS at 789.0")


# ── Day P&L ───────────────────────────────────────────────────────

async def test_day_pnl_when_flat(paper_bot):
    assert paper_bot.day_pnl() == {
        "closed": 0.0, "open_realized": 0.0, "unrealized": None,
        "total": 0.0, "marked": True, "position": None,
    }


async def test_day_pnl_marks_the_open_trade_to_the_last_price(paper_bot):
    paper_bot.symbol = "NSE:NIFTY50-INDEX"
    await enter(paper_bot, make_signal(stop=24495.0, t1=24510, t2=24520, t3=24530), price=24500.0)
    trade = paper_bot.active_trade
    await paper_bot._manage_trade(24510.0, T0 + timedelta(minutes=5))  # +1000 booked, 100 open
    paper_bot._last_price = 24507.0
    paper_bot._last_price_time = T0 + timedelta(minutes=6)

    pnl = paper_bot.day_pnl()

    assert pnl["closed"] == 0.0  # the trade is not closed yet
    assert pnl["open_realized"] == 1000.0
    assert pnl["unrealized"] == 700.0  # 100 x 7
    assert pnl["total"] == 1700.0 and pnl["marked"] is True
    assert pnl["position"] == {
        "trade_id": trade.id, "symbol": "NSE:NIFTY50-INDEX", "direction": "LONG",
        "entry_price": 24500.0, "remaining_qty": 100, "price": 24507.0,
        "price_time": (T0 + timedelta(minutes=6)).isoformat(),
        "realized_pnl": 1000.0, "unrealized_pnl": 700.0,
    }


async def test_day_pnl_never_guesses_a_missing_price(paper_bot):
    await enter(paper_bot)
    paper_bot._last_price = None
    pnl = paper_bot.day_pnl()
    assert (pnl["unrealized"], pnl["total"], pnl["marked"]) == (None, 0.0, False)
    assert pnl["position"]["price"] is None


async def test_a_short_is_marked_the_other_way(paper_bot):
    await enter(paper_bot, make_signal(direction=Direction.SHORT, stop=810.0, t1=780.0, t2=770.0, t3=760.0))
    paper_bot._last_price = 797.0
    assert paper_bot.day_pnl()["unrealized"] == 300.0


async def test_every_tick_pushes_the_day_pnl_while_a_trade_is_open(paper_bot):
    client = Client()
    paper_bot.add_websocket_client(client)
    await enter(paper_bot)
    at = int((T0 + timedelta(minutes=1) - T0.__class__(1970, 1, 1)).total_seconds())

    await paper_bot._on_tick({"symbol": EQ, "ltp": 803.0, "v": 0, "exch_time": at})
    assert client.sent[-1]["type"] == "pnl"
    assert client.sent[-1]["unrealized"] == 300.0 and client.sent[-1]["total"] == 300.0

    await paper_bot._on_tick({"symbol": EQ, "ltp": 798.0, "v": 0, "exch_time": at + 1})
    assert client.sent[-1]["unrealized"] == -200.0


async def test_no_pnl_messages_while_flat(paper_bot):
    client = Client()
    paper_bot.add_websocket_client(client)
    at = int((T0 - T0.__class__(1970, 1, 1)).total_seconds())
    await paper_bot._on_tick({"symbol": EQ, "ltp": 803.0, "v": 0, "exch_time": at})
    assert client.of_type("pnl") == []


async def test_a_switch_forgets_the_old_symbols_price(paper_bot):
    paper_bot._last_price = 803.0
    state = {"symbol": "NSE:INFY-EQ", "symbol_name": "Infosys", "version": 99}

    async def no_history(symbol):
        raise RuntimeError("offline")

    paper_bot._fetch_history_bundle = no_history
    await paper_bot._apply_symbol_state(state, reason="test", broadcast=False)
    assert paper_bot._last_price is None and paper_bot._last_price_time is None


def test_yesterdays_counters_are_not_todays_pnl():
    rm = RiskManager()
    rm.day = MONDAY_10_IST.date().isoformat()
    rm.daily_pnl = -1200.0
    assert rm.daily_pnl_on(MONDAY_10_IST) == -1200.0
    assert rm.daily_pnl_on(MONDAY_10_IST + timedelta(days=1)) == 0.0


# ── API ───────────────────────────────────────────────────────────

def test_status_reports_the_day_pnl(client):
    body = client.get("/api/status").json()
    assert body["day_pnl"]["total"] == body["today_pnl"]
    assert set(body["day_pnl"]) == {"closed", "open_realized", "unrealized", "total", "marked", "position"}


def test_exit_without_an_open_trade_is_a_409(client):
    response = client.post("/api/trades/exit", json={})
    assert response.status_code == 409
    assert response.json()["detail"] == "No trade is open"


def test_exit_needs_a_signed_in_user(db_path):
    from fastapi.testclient import TestClient

    import main

    with TestClient(main.app) as anonymous:
        response = anonymous.post("/api/trades/exit", json={})
    assert response.status_code in (401, 403)


def test_exit_reaches_the_engine(client, monkeypatch):
    import main

    seen = {}

    async def fake_exit(trade_id=None):
        seen["id"] = trade_id
        raise TradeExitError("partial", status=502)

    monkeypatch.setattr(main.app.state.bot, "exit_trade_now", fake_exit)
    response = client.post("/api/trades/exit", json={"trade_id": "TRD_1"})
    assert (response.status_code, response.json()["detail"], seen["id"]) == (502, "partial", "TRD_1")
