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


class TestDataSocketLogDir:
    """The SDK opens logs/fyersDataSocket.log while building the socket; an
    unwritable log directory used to crash the engine (startup_error
    "[Errno 13] Permission denied: '/app/logs/fyersDataSocket.log'")."""

    def test_writable_directory_is_used(self, tmp_path):
        from brokers.fyers_broker import DATA_SOCKET_LOG_FILE, writable_log_dir

        target = tmp_path / "logs"
        assert writable_log_dir(str(target)) == str(target)
        assert (target / DATA_SOCKET_LOG_FILE).exists()

    def test_unwritable_directory_falls_back_to_temp(self, tmp_path, monkeypatch):
        import tempfile

        from brokers.fyers_broker import writable_log_dir

        # A plain file where the directory should be: makedirs/open fail with
        # an OSError on every platform, like a root-owned bind mount in Docker.
        blocked = tmp_path / "logs"
        blocked.write_text("not a directory")
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
        chosen = writable_log_dir(str(blocked))
        assert chosen != str(blocked)
        assert chosen.startswith(str(tmp_path / "tmp"))

    def test_env_override(self, tmp_path, monkeypatch):
        from brokers.fyers_broker import writable_log_dir

        monkeypatch.setenv("FYERS_LOG_DIR", str(tmp_path / "custom"))
        assert writable_log_dir() == str(tmp_path / "custom")

    def test_socket_is_built_with_a_writable_log_path(self, tmp_path, monkeypatch):
        import os

        captured = {}

        class FakeDataWs:
            @staticmethod
            def FyersDataSocket(**kwargs):
                captured.update(kwargs)
                # What the real SDK does on construction.
                open(os.path.join(kwargs["log_path"], "fyersDataSocket.log"), "a").close()
                return object()

        # Reproduce production: the default ./logs exists but is unusable
        # (there it was a root-owned bind mount; here a plain file).
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("FYERS_LOG_DIR", raising=False)
        blocked = tmp_path / "logs"
        blocked.write_text("not a directory")
        import tempfile
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))

        broker = FyersBroker.__new__(FyersBroker)
        broker._loop = None
        broker._data_socket = None
        broker._pending_symbols = []
        broker._subscribed_symbols = set()
        broker.tick_callback = None
        socket = broker._build_data_socket(FakeDataWs, "APP-200:header.payload.sig")
        assert socket is not None
        assert captured["log_path"] not in ("logs", str(blocked))
        assert captured["access_token"] == "header.payload.sig"
