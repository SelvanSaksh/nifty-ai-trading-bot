"""POST /api/symbols/active — persisted, versioned, guarded, observable."""
import asyncio
import time
from datetime import datetime

import pytest

from config import settings
from models.enums import Direction
from models.trade import Trade


def _open_trade(symbol: str) -> Trade:
    return Trade(
        entry_time=datetime.now(),
        direction=Direction.LONG,
        entry_price=24500.0,
        quantity=50,
        initial_qty=50,
        remaining_qty=50,
        # Far away so a live tick cannot close this synthetic trade mid-test.
        stop_loss=20000.0,
        target_1=30000.0,
        target_2=31000.0,
        target_3=32000.0,
        signal_score=70,
        confidence=0.7,
        symbol=symbol,
    )


def test_active_symbol_reports_shared_state(client):
    state = client.get("/api/symbols/active").json()

    assert state["symbol"] == settings.TRADING_SYMBOL
    assert state["symbol_name"] == settings.TRADING_SYMBOL_NAME
    assert state["version"] == 0
    assert state["changed_at"] is None
    assert state["engine_leader"] is True
    assert state["bot_running"] is True
    assert state["in_sync"] is True


def test_switch_updates_candles_and_stays_stable(client):
    before = client.get("/api/symbols/active").json()

    resp = client.post(
        "/api/symbols/active",
        json={"symbol": "NSE:INFY-EQ", "symbol_name": "Infosys", "changed_by": "pytest"},
    )
    assert resp.status_code == 200, resp.text
    changed = resp.json()
    assert changed["symbol"] == "NSE:INFY-EQ"
    assert changed["version"] == before["version"] + 1
    assert changed["changed_by"] == "pytest"

    # Poll repeatedly — the value must not drift between requests.
    for _ in range(5):
        state = client.get("/api/symbols/active").json()
        assert state["symbol"] == "NSE:INFY-EQ"
        assert state["version"] == changed["version"]

        candles = client.get("/api/candles", params={"limit": 10})
        assert candles.headers["X-Symbol"] == "NSE:INFY-EQ"
        assert candles.headers["X-Active-Symbol"] == "NSE:INFY-EQ"
        body = candles.json()
        assert {c["symbol"] for c in body} == {"NSE:INFY-EQ"}
        # Candles really are Infosys levels, not Nifty 50 index levels.
        assert max(c["close"] for c in body) < 5000


def test_switching_back_to_the_same_symbol_does_not_churn_version(client):
    client.post("/api/symbols/active", json={"symbol": "NSE:INFY-EQ"})
    again = client.post("/api/symbols/active", json={"symbol": "NSE:INFY-EQ"})

    assert again.status_code == 200
    assert again.json()["message"] == "Symbol already active"
    assert again.json()["version"] == 1


def test_invalid_symbol_is_rejected(client):
    resp = client.post("/api/symbols/active", json={"symbol": "garbage"})
    assert resp.status_code == 422

    resp = client.post("/api/symbols/active", json={"symbol": "NSE:FAKE-EQ"})
    assert resp.status_code == 422
    assert "not in the watchlist" in resp.json()["detail"]

    assert client.get("/api/symbols/active").json()["symbol"] == settings.TRADING_SYMBOL
    assert client.get("/api/symbols/active").json()["version"] == 0


def test_switch_is_refused_while_a_trade_is_open(client):
    import main

    bot = main.app.state.bot
    bot.active_trade = _open_trade(bot.symbol)
    try:
        resp = client.post("/api/symbols/active", json={"symbol": "NSE:INFY-EQ"})
        assert resp.status_code == 409
        assert "open" in resp.json()["detail"]
        assert client.get("/api/symbols/active").json()["symbol"] == bot.symbol
    finally:
        bot.active_trade = None


def test_symbol_change_is_audited(client):
    client.post(
        "/api/symbols/active",
        json={"symbol": "NSE:INFY-EQ", "changed_by": "pytest"},
    )
    client.post(
        "/api/symbols/active",
        json={"symbol": "NSE:TCS-EQ", "changed_by": "pytest"},
    )

    changes = client.get("/api/symbols/history").json()["changes"]
    assert [c["to_symbol"] for c in changes] == ["NSE:TCS-EQ", "NSE:INFY-EQ"]
    assert changes[0]["from_symbol"] == "NSE:INFY-EQ"
    assert all(c["changed_by"] == "pytest" for c in changes)


def test_symbol_survives_a_process_restart(db_path):
    """Previously every restart silently reset to the configured default."""
    from fastapi.testclient import TestClient

    import main

    with TestClient(main.app) as first:
        resp = first.post(
            "/api/symbols/active",
            json={"symbol": "NSE:INFY-EQ", "changed_by": "pytest"},
        )
        assert resp.status_code == 200
        version = resp.json()["version"]

    with TestClient(main.app) as second:
        state = second.get("/api/symbols/active").json()
        assert state["symbol"] == "NSE:INFY-EQ"
        assert state["version"] == version

        candles = second.get("/api/candles", params={"limit": 10})
        assert candles.headers["X-Symbol"] == "NSE:INFY-EQ"
        assert max(c["close"] for c in candles.json()) < 5000


def test_change_written_by_another_process_converges(fast_client):
    """
    Simulates a second uvicorn worker / replica writing the shared state:
    the running engine must pick it up and serve matching candles.
    """
    from database import set_active_symbol_state

    external = asyncio.run(
        set_active_symbol_state(
            "NSE:INFY-EQ", "Infosys", updated_by="other-worker", reason="scaled-out-worker"
        )
    )
    assert external["symbol"] == "NSE:INFY-EQ"

    deadline = time.monotonic() + 10
    state = fast_client.get("/api/symbols/active").json()
    while time.monotonic() < deadline:
        state = fast_client.get("/api/symbols/active").json()
        if state["symbol"] == "NSE:INFY-EQ" and state["in_sync"]:
            break
        time.sleep(0.1)
    else:
        pytest.fail(f"engine never converged on the shared state: {state}")

    assert state["version"] == external["version"]

    candles = fast_client.get("/api/candles", params={"limit": 10})
    assert candles.headers["X-Symbol"] == "NSE:INFY-EQ"
    assert {c["symbol"] for c in candles.json()} == {"NSE:INFY-EQ"}


def test_websocket_hello_and_symbol_changed_event(client):
    with client.websocket_connect("/ws/live") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["active_symbol"]["symbol"] == settings.TRADING_SYMBOL

        resp = client.post("/api/symbols/active", json={"symbol": "NSE:INFY-EQ"})
        assert resp.status_code == 200

        event = None
        for _ in range(60):
            message = ws.receive_json()
            if message.get("type") == "symbol_changed":
                event = message
                break
        assert event is not None, "clients were never told the instrument changed"
        assert event["symbol"] == "NSE:INFY-EQ"
        assert event["old_symbol"] == settings.TRADING_SYMBOL
        assert event["version"] == 1


def test_health_and_status_expose_engine_state(client):
    health = client.get("/health").json()
    assert health["status"] == "healthy"
    assert health["engine_leader"] is True
    assert health["bot_running"] is True
    assert health["startup_error"] is None
    assert health["symbol"] == settings.TRADING_SYMBOL

    status = client.get("/api/status").json()
    assert status["symbol"] == settings.TRADING_SYMBOL
    assert status["symbol_version"] == 0
    assert status["engine_leader"] is True
    assert status["symbol_in_sync"] is True
