"""
State-changing endpoints must reject requests without a valid X-Admin-Key.

Before this guard existed, CORS was wide open (allow_origins=["*"]) and
nothing checked ADMIN_SECRET_KEY, so anyone who could reach the deployment
could start/stop the trading engine or switch the traded instrument.
"""
from fastapi.testclient import TestClient

import main
from tests.conftest import TEST_ADMIN_KEY


def _unauthenticated_client():
    return TestClient(main.app)


def test_start_bot_requires_admin_key(db_path):
    with _unauthenticated_client() as anon:
        resp = anon.post("/api/bot/start")
        assert resp.status_code == 401

    with TestClient(main.app, headers={"X-Admin-Key": "wrong"}) as wrong:
        resp = wrong.post("/api/bot/start")
        assert resp.status_code == 401


def test_stop_and_restart_require_admin_key(client):
    with _unauthenticated_client() as anon:
        assert anon.post("/api/bot/stop").status_code == 401
        assert anon.post("/api/bot/restart").status_code == 401


def test_set_active_symbol_requires_admin_key(client):
    with _unauthenticated_client() as anon:
        resp = anon.post("/api/symbols/active", json={"symbol": "NSE:INFY-EQ"})
        assert resp.status_code == 401

    # The correctly-authenticated fixture client still works.
    resp = client.post("/api/symbols/active", json={"symbol": "NSE:INFY-EQ"})
    assert resp.status_code == 200


def test_read_only_endpoints_do_not_require_admin_key(db_path):
    with _unauthenticated_client() as anon:
        assert anon.get("/health").status_code == 200
        assert anon.get("/api/status").status_code == 200
        assert anon.get("/api/symbols/active").status_code == 200


def test_mcp_start_stop_require_admin_key(client):
    # invoke_tool() always wraps a non-raising call as {"success": True,
    # "data": <tool result>} — the tool's own success/message is nested
    # under "data".
    resp = client.post("/mcp/invoke/start_bot", json={"confirm": True})
    assert resp.status_code == 200
    assert resp.json()["data"]["success"] is False  # already running, but auth passed

    with _unauthenticated_client() as anon:
        resp = anon.post("/mcp/invoke/start_bot", json={"confirm": True})
        assert resp.status_code == 401


def test_mcp_read_only_tools_do_not_require_admin_key(client):
    resp = client.post("/mcp/invoke/get_bot_status", json={})
    assert resp.status_code == 200

    with _unauthenticated_client() as anon:
        resp = anon.post("/mcp/invoke/get_bot_status", json={})
        assert resp.status_code == 200
