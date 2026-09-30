"""GET /api/candles — symbol-aware, verifiable, decoupled from the bot."""
from datetime import datetime, timedelta

from config import settings


def test_candles_are_labelled_with_their_symbol(client):
    resp = client.get(
        "/api/candles",
        params={"timeframe": "15m", "limit": 30, "symbol": settings.TRADING_SYMBOL},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list), "backwards-compatible bare list payload"
    assert len(body) == 30

    active = client.get("/api/symbols/active").json()
    assert resp.headers["X-Symbol"] == active["symbol"]
    assert resp.headers["X-Active-Symbol"] == active["symbol"]
    assert resp.headers["X-Symbol-Version"] == str(active["version"])
    assert resp.headers["X-Timeframe"] == "15m"
    assert resp.headers["X-Data-Source"] in ("live", "history")

    assert all(c["symbol"] == active["symbol"] for c in body)
    assert all(c["symbol_name"] == active["symbol_name"] for c in body)
    assert all(c["timeframe"] == "15m" for c in body)


def test_candles_for_a_requested_symbol_ignore_the_active_one(client):
    resp = client.get(
        "/api/candles",
        params={"symbol": "NSE:INFY-EQ", "timeframe": "15m", "limit": 20},
    )

    assert resp.status_code == 200
    active = client.get("/api/symbols/active").json()

    # Viewing instrument and trading instrument are independent.
    assert active["symbol"] == settings.TRADING_SYMBOL
    assert resp.headers["X-Symbol"] == "NSE:INFY-EQ"
    assert resp.headers["X-Active-Symbol"] == active["symbol"]
    assert resp.headers["X-Symbol"] != resp.headers["X-Active-Symbol"]

    body = resp.json()
    assert len(body) == 20
    assert all(c["symbol"] == "NSE:INFY-EQ" for c in body)
    # Equity price levels, not index levels — data matches the label.
    assert all(c["close"] < 5000 for c in body)


def test_repeated_polls_never_change_the_instrument(client):
    """The reported bug: touching timeframe flipped symbol + data."""
    params = {"timeframe": "5m", "limit": 10, "symbol": settings.TRADING_SYMBOL}
    first = client.get("/api/candles", params=params)
    for _ in range(4):
        again = client.get("/api/candles", params=params)
        assert again.headers["X-Symbol"] == first.headers["X-Symbol"]
        assert again.headers["X-Symbol-Version"] == first.headers["X-Symbol-Version"]
        assert again.headers["X-Active-Symbol"] == first.headers["X-Active-Symbol"]
        assert {c["symbol"] for c in again.json()} == {
            first.json()[0]["symbol"]
        }


def test_detail_envelope(client):
    resp = client.get(
        "/api/candles/detail",
        params={"timeframe": "5m", "limit": 5, "symbol": "NSE:INFY-EQ"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["symbol"] == "NSE:INFY-EQ"
    assert body["symbol_name"] == "Infosys"
    assert body["active_symbol"] == settings.TRADING_SYMBOL
    assert body["timeframe"] == "5m"
    assert body["limit"] == 5
    assert body["source"] in ("live", "history")
    assert body["count"] == len(body["candles"]) == 5
    assert body["error"] is None
    assert body["coverage"]["start"] <= body["coverage"]["end"]


def test_timeframe_aliases_resolve_to_the_same_bars(client):
    def with_tf(tf: str) -> dict:
        return {"timeframe": tf, "limit": 5, "symbol": settings.TRADING_SYMBOL}

    canonical = client.get("/api/candles", params=with_tf("15m"))
    alias = client.get("/api/candles", params=with_tf("15"))
    hourly = client.get("/api/candles", params=with_tf("60"))

    assert canonical.status_code == alias.status_code == hourly.status_code == 200
    assert canonical.headers["X-Timeframe"] == alias.headers["X-Timeframe"] == "15m"
    assert hourly.headers["X-Timeframe"] == "1h"


def test_end_time_filters_the_window(client):
    end = datetime.now() - timedelta(hours=6)
    resp = client.get(
        "/api/candles",
        params={
            "timeframe": "15m",
            "limit": 40,
            "symbol": settings.TRADING_SYMBOL,
            "end_time": end.isoformat(),
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body
    assert all(datetime.fromisoformat(c["timestamp"]) <= end for c in body)


def test_invalid_requests_return_422(client):
    base = {"symbol": settings.TRADING_SYMBOL, "timeframe": "15m"}
    assert client.get("/api/candles", params={**base, "timeframe": "7m"}).status_code == 422
    assert client.get("/api/candles", params={**base, "limit": 0}).status_code == 422
    assert client.get("/api/candles", params={**base, "limit": 5000}).status_code == 422
    assert (
        client.get("/api/candles", params={**base, "end_time": "yesterday"}).status_code
        == 422
    )
    assert client.get("/api/candles", params={**base, "symbol": "nope"}).status_code == 422
    assert (
        client.get("/api/candles", params={**base, "timeframe": "abc"}).status_code == 422
    )


def test_symbol_and_timeframe_are_required_and_never_inferred(client):
    """The FE owns the instrument and the interval: both must be sent."""
    missing_symbol = client.get(
        "/api/candles", params={"timeframe": "15m", "limit": 5}
    )
    assert missing_symbol.status_code == 422

    missing_timeframe = client.get(
        "/api/candles", params={"symbol": settings.TRADING_SYMBOL, "limit": 5}
    )
    assert missing_timeframe.status_code == 422

    blank_symbol = client.get(
        "/api/candles", params={"symbol": "   ", "timeframe": "15m", "limit": 5}
    )
    assert blank_symbol.status_code == 422

    blank_timeframe = client.get(
        "/api/candles", params={"symbol": settings.TRADING_SYMBOL, "timeframe": " "}
    )
    assert blank_timeframe.status_code == 422

    detail = client.get("/api/candles/detail", params={"limit": 5})
    assert detail.status_code == 422


def test_active_symbol_endpoint_and_candles_agree(client):
    """Every read of the active symbol must return the same value."""
    values = set()
    for _ in range(6):
        state = client.get("/api/symbols/active").json()
        candles = client.get(
            "/api/candles",
            params={
                "limit": 5,
                "symbol": settings.TRADING_SYMBOL,
                "timeframe": "15m",
            },
        )
        assert candles.headers["X-Active-Symbol"] == state["symbol"]
        assert candles.headers["X-Symbol-Version"] == str(state["version"])
        values.add((state["symbol"], state["version"]))
    assert len(values) == 1, f"active symbol churned between polls: {values}"
