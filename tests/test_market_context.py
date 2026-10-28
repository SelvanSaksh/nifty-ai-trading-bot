"""GET /api/market-context never presents made-up options data."""
from features.options import options_analyzer


def test_options_fields_are_null_while_not_connected(client):
    body = client.get("/api/market-context").json()

    assert body["options_available"] is False
    for key in ("pcr", "vix_regime", "max_pain", "call_oi", "put_oi",
                "nearest_expiry", "bias"):
        assert body[key] is None, key
    assert body["oi_change"] == {"call": None, "put": None}
    # The news part is real and still reported.
    assert body["news_blackout"] is False
    assert body["active_blackouts"] == []


async def test_options_analyzer_reports_no_context():
    assert await options_analyzer.get_context() is None
    assert options_analyzer.get_bias(None) is None
    assert options_analyzer.get_size_multiplier(None) == 1.0
