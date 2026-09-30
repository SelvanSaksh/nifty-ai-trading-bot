"""Token expiry detection: status reporting, login return URL, history fetch."""
from datetime import datetime, timedelta

import pytest

import fyers_auth
from brokers.fyers_broker import FyersAuthError, FyersBroker
from config import settings


@pytest.fixture(autouse=True)
def _isolated_auth_state(tmp_path, monkeypatch):
    """The probe cache and the token file are process-wide: isolate both."""
    fyers_auth.reset_token_probe()
    monkeypatch.setattr(fyers_auth, "FYERS_TOKEN_FILE", str(tmp_path / "fyers_token.txt"))
    yield
    fyers_auth.reset_token_probe()


class FakeResponse:
    def __init__(self, status: int, body: str = "", payload=None):
        self.status = status
        self._body = body
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def text(self):
        return self._body

    async def json(self, content_type=None):
        return self._payload


class FakeSession:
    def __init__(self, response: FakeResponse):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, *args, **kwargs):
        return self._response


def _patch_http(monkeypatch, response: FakeResponse):
    """Route every `aiohttp.ClientSession` use in fyers_auth to a canned reply."""
    import aiohttp

    monkeypatch.setattr(
        aiohttp, "ClientSession", lambda *args, **kwargs: FakeSession(response)
    )


class TestVerifyToken:
    async def test_rejected_by_fyers(self, monkeypatch):
        _patch_http(monkeypatch, FakeResponse(401, '{"s":"error"}'))
        assert await fyers_auth.verify_token("APP:expired") is False

    async def test_accepted_by_fyers(self, monkeypatch):
        _patch_http(monkeypatch, FakeResponse(200, '{"s":"ok"}', {"s": "ok"}))
        assert await fyers_auth.verify_token("APP:live") is True

    async def test_network_failure_is_unknown_not_expired(self, monkeypatch):
        def boom(*args, **kwargs):
            raise OSError("fyers unreachable")

        import aiohttp

        monkeypatch.setattr(aiohttp, "ClientSession", boom)
        assert await fyers_auth.verify_token("APP:live") is None

    async def test_answer_is_cached_until_it_is_forced(self, monkeypatch):
        import aiohttp

        calls = []

        def factory(*args, **kwargs):
            calls.append(1)
            return FakeSession(FakeResponse(401, '{"s":"error"}'))

        monkeypatch.setattr(aiohttp, "ClientSession", factory)

        assert await fyers_auth.verify_token("APP:expired") is False
        assert await fyers_auth.verify_token("APP:expired") is False
        assert len(calls) == 1, "the verdict must be reused inside the TTL"

        assert await fyers_auth.verify_token("APP:expired", force=True) is False
        assert len(calls) == 2, "force=True must talk to Fyers again"


class TestAuthStatus:
    async def test_expired_env_token_reports_unauthenticated(self, monkeypatch):
        monkeypatch.setattr(settings, "FYERS_ACCESS_TOKEN", "APP:expired")
        monkeypatch.setattr(
            fyers_auth, "verify_token", lambda *args, **kwargs: _resolved(False)
        )

        status = await fyers_auth.auth_status()

        assert status["authenticated"] is False
        assert status["source"] == "environment"
        assert status["reason"] == "Fyers access token expired"
        assert status["login_available"] is True

    async def test_live_token_reports_authenticated(self, monkeypatch):
        monkeypatch.setattr(settings, "FYERS_ACCESS_TOKEN", "APP:live")
        monkeypatch.setattr(
            fyers_auth, "verify_token", lambda *args, **kwargs: _resolved(True)
        )

        status = await fyers_auth.auth_status()

        assert status["authenticated"] is True
        assert status["reason"] is None

    async def test_missing_token(self, monkeypatch):
        monkeypatch.setattr(settings, "FYERS_ACCESS_TOKEN", "")
        monkeypatch.setattr(fyers_auth, "FYERS_TOKEN_FILE", "/nonexistent/token.txt")

        status = await fyers_auth.auth_status()

        assert status["authenticated"] is False
        assert status["reason"] == "Fyers token missing"

    async def test_unreachable_fyers_does_not_invalidate_the_session(self, monkeypatch):
        monkeypatch.setattr(settings, "FYERS_ACCESS_TOKEN", "APP:live")
        monkeypatch.setattr(
            fyers_auth, "verify_token", lambda *args, **kwargs: _resolved(None)
        )

        status = await fyers_auth.auth_status()

        assert status["authenticated"] is True


def _resolved(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner()


class TestTokenSelection:
    """The token on the wire must be the newest one we hold."""

    @staticmethod
    def _write_token(path, token: str, days: int):
        path.write_text(
            f"{token}\n"
            f"generated_at:{datetime.now().isoformat()}\n"
            f"expires_at:{(datetime.now() + timedelta(days=days)).isoformat()}\n",
            encoding="utf-8",
        )

    def test_a_fresh_login_wins_over_the_environment_value(self, tmp_path, monkeypatch):
        token_file = tmp_path / "rotated.txt"
        self._write_token(token_file, "77M2C2QWOQ-200:from-login", 10)
        monkeypatch.setattr(fyers_auth, "FYERS_TOKEN_FILE", str(token_file))
        monkeypatch.setattr(settings, "FYERS_APP_ID", "77M2C2QWOQ-200")
        monkeypatch.setattr(settings, "FYERS_ACCESS_TOKEN", "77M2C2QWOQ-200:from-env")

        token, _, source = fyers_auth.active_token()

        assert token.endswith("from-login")
        assert source == "file"

    def test_the_environment_value_bootstraps_the_first_login(self, monkeypatch):
        monkeypatch.setattr(settings, "FYERS_ACCESS_TOKEN", "77M2C2QWOQ-200:from-env")
        monkeypatch.setattr(settings, "FYERS_APP_ID", "77M2C2QWOQ-200")

        token, _, source = fyers_auth.active_token()

        assert token.endswith("from-env")
        assert source == "environment"

    def test_a_stale_file_token_does_not_shadow_the_environment(self, tmp_path, monkeypatch):
        token_file = tmp_path / "stale.txt"
        self._write_token(token_file, "77M2C2QWOQ-200:stale", -1)
        monkeypatch.setattr(fyers_auth, "FYERS_TOKEN_FILE", str(token_file))
        monkeypatch.setattr(settings, "FYERS_APP_ID", "77M2C2QWOQ-200")
        monkeypatch.setattr(settings, "FYERS_ACCESS_TOKEN", "77M2C2QWOQ-200:from-env")

        token, _, source = fyers_auth.active_token()

        assert token.endswith("from-env")
        assert source == "environment"


class TestReturnUrl:
    def test_own_frontend_is_allowed(self):
        assert fyers_auth.safe_return_url("http://localhost:5173/") == "http://localhost:5173/"

    def test_foreign_origin_is_rejected(self):
        assert fyers_auth.safe_return_url("https://evil.example/?x=1") is None

    def test_non_http_scheme_is_rejected(self):
        assert fyers_auth.safe_return_url("javascript:alert(1)") is None

    def test_configured_frontend_is_allowed(self, monkeypatch):
        monkeypatch.setattr(settings, "FRONTEND_URL", "https://app.quantumvora.com")
        url = "https://app.quantumvora.com/terminal"
        assert fyers_auth.safe_return_url(url) == url

    def test_login_url_carries_the_return_target(self, monkeypatch):
        monkeypatch.setattr(settings, "FYERS_APP_ID", "77M2C2QWOQ-200")
        monkeypatch.setattr(settings, "FYERS_SECRET", "secret")

        url = fyers_auth.get_auth_url("http://localhost:5173/")

        assert "state=http%3A%2F%2Flocalhost%3A5173%2F" in url

    def test_login_url_falls_back_to_the_default_state(self, monkeypatch):
        monkeypatch.setattr(settings, "FYERS_APP_ID", "77M2C2QWOQ-200")
        monkeypatch.setattr(settings, "FYERS_SECRET", "secret")

        assert "state=niftybot" in fyers_auth.get_auth_url("https://evil.example/")


class TestHistoryFetch:
    async def test_text_plain_rejection_raises_an_auth_error(self, monkeypatch):
        """Fyers answers 404/text/plain for a dead token — never a JSON body."""
        monkeypatch.setattr(settings, "FYERS_ACCESS_TOKEN", "APP:expired")
        monkeypatch.setattr(fyers_auth, "verify_token", lambda *a, **k: _resolved(False))

        broker = FyersBroker()
        _stub_session(monkeypatch, broker, FakeResponse(404, ""))

        with pytest.raises(FyersAuthError):
            await broker.get_historical_candles(symbol="NSE:NIFTY50-INDEX", timeframe="15")

    async def test_valid_history_is_parsed(self, monkeypatch):
        body = '{"s":"ok","candles":[[1767139200,26000,26100,25900,26050,1234]]}'
        broker = FyersBroker()
        _stub_session(monkeypatch, broker, FakeResponse(200, body, None))

        candles = await broker.get_historical_candles(
            symbol="NSE:NIFTY50-INDEX", timeframe="15"
        )

        assert len(candles) == 1
        assert candles[0].close == 26050
        assert candles[0].symbol == "NSE:NIFTY50-INDEX"

    async def test_non_auth_failure_does_not_claim_the_token_expired(self, monkeypatch):
        monkeypatch.setattr(settings, "FYERS_ACCESS_TOKEN", "APP:live")
        monkeypatch.setattr(fyers_auth, "verify_token", lambda *a, **k: _resolved(True))

        broker = FyersBroker()
        _stub_session(monkeypatch, broker, FakeResponse(404, ""))

        with pytest.raises(ConnectionError) as excinfo:
            await broker.get_historical_candles(symbol="NSE:NIFTY50-INDEX", timeframe="15")

        assert not isinstance(excinfo.value, FyersAuthError)


def _stub_session(monkeypatch, broker: FyersBroker, response: FakeResponse):
    async def _ensure():
        return FakeSession(response)

    monkeypatch.setattr(broker, "_ensure_session", _ensure)


class TestSessionRefresh:
    async def test_new_token_rebuilds_the_session(self, monkeypatch):
        monkeypatch.setattr(settings, "FYERS_ACCESS_TOKEN", "APP:first")
        broker = FyersBroker()

        first = await broker._ensure_session()
        assert broker._auth_token == broker._get_auth_header()

        monkeypatch.setattr(settings, "FYERS_ACCESS_TOKEN", "APP:second")
        second = await broker._ensure_session()

        assert second is not first
        assert first.closed, "the session holding the old token must be closed"
        assert broker._auth_token == broker._get_auth_header()
        assert broker._headers["Authorization"] == broker._auth_token
        await second.close()

    async def test_reauthenticate_drops_the_cached_session(self, monkeypatch):
        monkeypatch.setattr(settings, "FYERS_ACCESS_TOKEN", "APP:first")
        broker = FyersBroker()
        await broker._ensure_session()

        await broker.reauthenticate()

        assert broker.session is None
        assert broker._auth_token is None


class TestLoginEndpoints:
    """The web client's redirect loop stands on these two endpoints."""

    @pytest.fixture(autouse=True)
    def _credentials(self, monkeypatch):
        monkeypatch.setattr(settings, "FYERS_APP_ID", "77M2C2QWOQ-200")
        monkeypatch.setattr(settings, "FYERS_SECRET", "secret")
        monkeypatch.setattr(settings, "FYERS_ACCESS_TOKEN", "APP:test")
        # Keep the status probe off the network.
        monkeypatch.setattr(fyers_auth, "verify_token", lambda *a, **k: _resolved(True))

    def test_status_reports_a_verified_session_without_leaking_it(self, client):
        response = client.get("/api/auth/status")

        assert response.status_code == 200
        payload = response.json()
        assert payload["authenticated"] is True
        assert "APP:test" not in response.text

    def test_login_round_trips_the_return_target(self, client):
        response = client.get(
            "/api/auth/login",
            params={"return_to": "http://localhost:5173/"},
            follow_redirects=False,
        )

        assert response.status_code == 307
        assert "state=http%3A%2F%2Flocalhost%3A5173%2F" in response.headers["location"]

    def test_login_refuses_a_foreign_return_target(self, client):
        response = client.get(
            "/api/auth/login",
            params={"return_to": "https://evil.example/"},
            follow_redirects=False,
        )

        assert response.status_code == 307
        assert "state=niftybot" in response.headers["location"]
