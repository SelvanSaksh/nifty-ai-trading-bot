"""GET /api/risk/status reports the limits the gate checks against."""
from datetime import datetime

from config import settings
from risk_manager import RiskManager

# A Wednesday, inside the session window.
IN_SESSION = datetime(2026, 10, 21, 11, 0)


def test_risk_status_reports_the_limits(client, monkeypatch):
    monkeypatch.setattr(settings, "CAPITAL", 200_000.0)
    monkeypatch.setattr(settings, "MAX_DAILY_LOSS", 0.02)
    monkeypatch.setattr(settings, "MAX_WEEKLY_LOSS", 0.05)
    monkeypatch.setattr(settings, "MAX_TRADES_PER_DAY", 4)

    body = client.get("/api/risk/status").json()

    assert body["daily_loss_limit"] == -4000.0
    assert body["weekly_loss_limit"] == -10000.0
    assert body["max_trades_per_day"] == 4
    # The fields that were already there are unchanged.
    for key in ("can_trade", "reason", "daily_pnl", "weekly_pnl",
                "trades_today", "consecutive_losses", "cooldown_until"):
        assert key in body


async def test_gate_blocks_exactly_at_the_reported_limits(monkeypatch):
    rm = RiskManager()
    rm.day = IN_SESSION.date().isoformat()
    year, week, _ = IN_SESSION.isocalendar()
    rm.week = f"{year}-W{week:02d}"

    async def no_news():
        return False, []

    monkeypatch.setattr(rm, "_get_news_status", no_news)
    monkeypatch.setattr(rm, "save", _noop)

    assert (await rm.can_trade(IN_SESSION))[0] is True

    rm.daily_pnl = rm.daily_loss_limit
    ok, reason = await rm.can_trade(IN_SESSION)
    assert ok is False and reason.startswith("Daily loss limit hit")

    rm.daily_pnl = 0.0
    rm.weekly_pnl = rm.weekly_loss_limit
    ok, reason = await rm.can_trade(IN_SESSION)
    assert ok is False and reason.startswith("Weekly loss limit hit")

    rm.weekly_pnl = 0.0
    rm.trades_today = rm.max_trades_per_day
    ok, reason = await rm.can_trade(IN_SESSION)
    assert ok is False and reason.startswith("Max trades per day reached")


async def _noop(*_args, **_kwargs):
    return None
