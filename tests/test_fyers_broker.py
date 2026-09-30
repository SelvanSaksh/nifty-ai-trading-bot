"""Fyers broker/auth helpers: live endpoint config, tick mapping, token guard."""
from datetime import datetime, timedelta

from brokers.fyers_broker import FyersBroker, normalize_tick
from config import settings


class TestNormalizeTick:
    def test_maps_symbol_update_payload(self):
        tick = normalize_tick({
            "symbol": "NSE:NIFTY50-INDEX",
            "ltp": 26100.5,
            "vol_traded_today": 1234,
            "open_price": 26000,
            "high_price": 26150,
            "low_price": 25980,
            "prev_close_price": 25900,
            "type": "if",
        })

        assert tick["symbol"] == "NSE:NIFTY50-INDEX"
        assert tick["ltp"] == 26100.5
        assert tick["v"] == 1234
        assert tick["open"] == 26000.0
        assert tick["prev_close"] == 25900.0

    def test_drops_control_frames(self):
        assert normalize_tick({"code": 1605, "s": "ok", "type": "sub"}) is None
        assert normalize_tick({"symbol": "NSE:X-EQ", "ltp": None}) is None
        assert normalize_tick("not a dict") is None


class TestLiveEndpointConfig:
    def test_rest_base_url_is_fyers_production_host(self):
        assert FyersBroker.BASE_URL.startswith("https://")
        assert FyersBroker.BASE_URL == settings.FYERS_API_URL

    def test_candles_use_the_data_host_not_the_trading_host(self):
        """`api/v3/history` is a 404; candles live under /data."""
        assert FyersBroker.DATA_URL == settings.FYERS_DATA_URL
        assert FyersBroker.DATA_URL == "https://api-t1.fyers.in/data"
        assert FyersBroker.DATA_URL != FyersBroker.BASE_URL

    def test_auth_url_uses_configured_app_and_redirect(self, monkeypatch):
        from fyers_auth import get_auth_url

        monkeypatch.setattr(settings, "FYERS_APP_ID", "77M2C2QWOQ-200")
        monkeypatch.setattr(
            settings, "FYERS_REDIRECT_URI", "https://api.trading.quantumvora.com/api/auth/callback"
        )

        url = get_auth_url()

        assert url.startswith(f"{settings.FYERS_API_URL}/generate-authcode")
        assert "client_id=77M2C2QWOQ-200" in url
        assert "redirect_uri=https%3A%2F%2Fapi.trading.quantumvora.com%2Fapi%2Fauth%2Fcallback" in url


class TestTokenBelongsToApp:
    def test_token_from_other_app_is_rejected(self, tmp_path, monkeypatch):
        import fyers_auth

        stale = tmp_path / "fyers_token.txt"
        expires = datetime.now() + timedelta(days=10)
        stale.write_text(
            f"5LITWFWCEU-100:eyJhbGciOiJIUzI1NiJ9.stale.sig\n"
            f"generated_at:{datetime.now().isoformat()}\n"
            f"expires_at:{expires.isoformat()}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(fyers_auth, "FYERS_TOKEN_FILE", str(stale))
        monkeypatch.setattr(settings, "FYERS_APP_ID", "77M2C2QWOQ-200")

        token, expires_at = fyers_auth.load_token()

        assert token == ""
        assert expires_at is None

    def test_current_app_token_is_accepted(self, tmp_path, monkeypatch):
        import fyers_auth

        token_file = tmp_path / "fyers_token.txt"
        expires = datetime.now() + timedelta(days=10)
        token_file.write_text(
            f"77M2C2QWOQ-200:eyJhbGciOiJIUzI1NiJ9.live.sig\n"
            f"generated_at:{datetime.now().isoformat()}\n"
            f"expires_at:{expires.isoformat()}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(fyers_auth, "FYERS_TOKEN_FILE", str(token_file))
        monkeypatch.setattr(settings, "FYERS_APP_ID", "77M2C2QWOQ-200")

        token, expires_at = fyers_auth.load_token()

        assert token.startswith("77M2C2QWOQ-200:")
        assert expires_at is not None
