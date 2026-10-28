"""Market status: session hours plus live-tick evidence, and GET /api/config."""
from datetime import datetime, timedelta

import pytest

from config import settings
from utils.market_status import (
    LIVE_TICK_MAX_AGE,
    is_live_session_tick,
    market_status,
    next_session_open,
)

# Naive UTC. 2026-10-28 is a Wednesday; IST = UTC + 5:30.
WED_11_IST = datetime(2026, 10, 28, 5, 30)
WED_0900_IST = datetime(2026, 10, 28, 3, 30)
WED_1600_IST = datetime(2026, 10, 28, 10, 30)
FRI_1600_IST = datetime(2026, 10, 30, 10, 30)
SAT_11_IST = datetime(2026, 10, 31, 5, 30)


def test_open_with_a_fresh_live_tick():
    status = market_status(WED_11_IST, WED_11_IST - timedelta(seconds=5))
    assert status["state"] == "open" and status["reason"] is None
    assert status["closes_at"] == "2026-10-28T10:00:00"  # 15:30 IST
    assert status["next_open"] is None


@pytest.mark.parametrize("last_tick", [
    None,                                                    # never ticked
    WED_11_IST - LIVE_TICK_MAX_AGE - timedelta(seconds=1),   # gone quiet
])
def test_session_hours_without_live_ticks(last_tick):
    status = market_status(WED_11_IST, last_tick)
    assert (status["state"], status["reason"]) == ("no_ticks", "no_live_ticks")


def test_tick_exactly_at_the_limit_still_counts():
    assert market_status(WED_11_IST, WED_11_IST - LIVE_TICK_MAX_AGE)["state"] == "open"


@pytest.mark.parametrize("now, reason, next_open", [
    (WED_0900_IST, "before_open", "2026-10-28T03:45:00"),
    (WED_1600_IST, "after_close", "2026-10-29T03:45:00"),
    (FRI_1600_IST, "after_close", "2026-11-02T03:45:00"),  # Monday
    (SAT_11_IST, "weekend", "2026-11-02T03:45:00"),
])
def test_closed(now, reason, next_open):
    status = market_status(now, now - timedelta(seconds=1))  # even with ticks
    assert (status["state"], status["reason"], status["next_open"]) == ("closed", reason, next_open)
    assert status["closes_at"] is None


def test_next_open_at_the_open_itself_is_the_next_day():
    assert next_session_open(datetime(2026, 10, 28, 3, 45)) == datetime(2026, 10, 29, 3, 45)


def test_only_in_session_ticks_prove_a_live_market():
    received = WED_11_IST
    assert is_live_session_tick(WED_11_IST - timedelta(seconds=2), received)
    # Fyers keeps sending index ticks after the close.
    assert not is_live_session_tick(datetime(2026, 10, 28, 10, 5), datetime(2026, 10, 28, 10, 5))
    # A stale exchange time from an earlier session.
    assert not is_live_session_tick(datetime(2026, 10, 27, 5, 30), received)


def test_status_reports_the_market(client):
    market = client.get("/api/status").json()["market"]
    assert market["state"] in {"open", "no_ticks", "closed"}
    assert set(market) == {"state", "reason", "closes_at", "next_open", "last_live_tick"}


def test_config_is_the_live_settings_without_secrets(client, monkeypatch):
    monkeypatch.setattr(settings, "MIN_SCORE_THRESHOLD", 61)
    monkeypatch.setattr(settings, "FYERS_SECRET", "s3cret-value")
    body = client.get("/api/config").json()

    assert body["min_score_threshold"] == 61
    assert body["capital"] == settings.CAPITAL
    assert body["square_off_time"] == settings.SQUARE_OFF_TIME
    text = str(body)
    for secret in ("s3cret-value", settings.ADMIN_SECRET_KEY):
        assert secret not in text
    assert not any("secret" in k or "token" in k or "key" in k for k in body)
