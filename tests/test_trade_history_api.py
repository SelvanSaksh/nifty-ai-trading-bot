"""Trade history paging, single-trade lookup and the daily P&L history."""
from datetime import datetime, timedelta

import pytest

from database import get_closed_trades, get_recent_trades, init_db, save_trade
from models.enums import Direction, SignalResult, TradeStatus
from models.trade import Trade
from utils.pnl_history import daily_pnl

# 2026-11-16 10:00 IST = 04:30 UTC.
MON = datetime(2026, 11, 16, 4, 30)


def closed(trade_id, exit_at, pnl, result=None, entry_at=None):
    if result is None and pnl != 0:
        result = SignalResult.WIN if pnl > 0 else SignalResult.LOSS
    return Trade(
        id=trade_id, entry_time=entry_at or exit_at - timedelta(minutes=30),
        direction=Direction.LONG, entry_price=800.0, quantity=10, initial_qty=10,
        remaining_qty=0, stop_loss=790.0, target_1=815.0, target_2=825.0,
        target_3=835.0, status=TradeStatus.CLOSED, exit_price=800.0 + pnl / 10,
        exit_time=exit_at, exit_reason="TARGET_3" if pnl > 0 else "STOP_LOSS",
        realized_pnl=pnl, result=result, symbol="NSE:SBIN-EQ", symbol_name="SBI",
        signal_score=70, confidence=0.7, paper_trade=True,
    )


# ── Daily P&L (pure) ──────────────────────────────────────────────

def test_trades_are_grouped_by_the_ist_day_they_closed():
    trades = [
        closed("A", MON, 500.0),
        closed("B", MON + timedelta(hours=2), -200.0),
        # 23:00 IST on Monday is still Monday in IST (17:30 UTC).
        closed("C", datetime(2026, 11, 16, 17, 30), 100.0),
        # 00:30 IST Tuesday = 19:00 UTC Monday: an IST Tuesday close.
        closed("D", datetime(2026, 11, 16, 19, 0), -900.0),
        closed("E", MON + timedelta(days=2), 1000.0),
    ]
    result = daily_pnl(trades, days=30)
    assert result["days"] == [
        {"day": "2026-11-16", "trades": 3, "wins": 2, "losses": 1, "pnl": 400.0, "cumulative": 400.0},
        {"day": "2026-11-17", "trades": 1, "wins": 0, "losses": 1, "pnl": -900.0, "cumulative": -500.0},
        {"day": "2026-11-18", "trades": 1, "wins": 1, "losses": 0, "pnl": 1000.0, "cumulative": 500.0},
    ]
    assert result["total_pnl"] == 500.0
    assert result["total_trades"] == 5
    assert result["trading_days"] == 3
    assert result["max_drawdown"] == 900.0  # 400 -> -500
    assert result["best_day"]["day"] == "2026-11-18"
    assert result["worst_day"]["day"] == "2026-11-17"


def test_the_window_keeps_the_running_total_of_earlier_days():
    trades = [closed(str(i), MON + timedelta(days=i), 100.0) for i in range(5)]
    result = daily_pnl(trades, days=2)
    assert [d["cumulative"] for d in result["days"]] == [400.0, 500.0]
    assert result["total_pnl"] == 500.0 and result["trading_days"] == 5


def test_a_trade_closed_outside_the_bot_counts_without_a_result():
    outside = closed("X", MON, 0.0)
    outside.result = None
    outside.exit_reason = "CLOSED_OUTSIDE_BOT"
    row = daily_pnl([outside], days=5)["days"][0]
    assert (row["trades"], row["wins"], row["losses"], row["pnl"]) == (1, 0, 0, 0.0)


def test_no_trades():
    assert daily_pnl([], days=30) == {
        "days": [], "total_pnl": 0.0, "total_trades": 0, "trading_days": 0,
        "max_drawdown": 0.0, "best_day": None, "worst_day": None,
    }


# ── Database ──────────────────────────────────────────────────────

async def test_trades_page_newest_first(db_path):
    await init_db()
    for i in range(5):
        await save_trade(closed(f"T{i}", MON + timedelta(hours=i), 10.0))
    first = await get_recent_trades(2, 0)
    second = await get_recent_trades(2, 2)
    last = await get_recent_trades(2, 4)
    assert [t.id for t in first + second + last] == ["T4", "T3", "T2", "T1", "T0"]


async def test_only_closed_trades_make_the_history(db_path):
    await init_db()
    await save_trade(closed("C1", MON, 10.0))
    still_open = closed("O1", MON, 0.0)
    still_open.status = TradeStatus.PARTIAL_1
    await save_trade(still_open)
    assert [t.id for t in await get_closed_trades()] == ["C1"]


# ── API ───────────────────────────────────────────────────────────

@pytest.fixture()
async def seeded(db_path):
    await init_db()
    for i in range(3):
        await save_trade(closed(f"T{i}", MON + timedelta(days=i), 100.0 * (i + 1)))


def test_trade_paging_and_lookup(client, seeded):
    assert [t["id"] for t in client.get("/api/trades?limit=2").json()] == ["T2", "T1"]
    assert [t["id"] for t in client.get("/api/trades?limit=2&offset=2").json()] == ["T0"]
    assert client.get("/api/trades/T1").json()["realized_pnl"] == 200.0
    missing = client.get("/api/trades/NOPE")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "Trade NOPE not found"


def test_daily_pnl_api(client, seeded):
    body = client.get("/api/pnl/daily?days=2").json()
    assert [d["day"] for d in body["days"]] == ["2026-11-17", "2026-11-18"]
    assert [d["cumulative"] for d in body["days"]] == [300.0, 600.0]
    assert body["total_pnl"] == 600.0 and body["trading_days"] == 3
