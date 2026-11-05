"""
Dashboard sign-in: the 6-digit PIN, sessions, the wrong-PIN lockout, and
which credentials the control endpoints accept.
"""
import io
import sqlite3
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

import main
from config import settings
from utils import auth

PIN = "482913"
WRONG = "482914"
# Few iterations, so the many sign-ins here stay fast; the format is the
# one set-pin writes.
STORED = auth.hash_pin(PIN, iterations=1000)


class FakeClock:
    """Stands in for the ``time`` module inside utils.auth: monotonic time
    for the lockout, moved by [advance]."""

    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture()
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(auth, "time", fake)
    return fake


@pytest.fixture()
def signin(db_path, monkeypatch, clock):
    """Sign-in set up as on a server."""
    monkeypatch.setattr(settings, "ADMIN_PIN_HASH", STORED)
    return clock


@pytest.fixture()
def anon(signin):
    """A client without the admin key, with the engine running."""
    with TestClient(main.app) as client:
        yield client


def _login(client, pin):
    return client.post("/api/session/login", json={"pin": pin})


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _sign_in(client):
    resp = _login(client, PIN)
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


# ── The PIN hash ────────────────────────────────────────────────

def test_the_hash_is_salted_pbkdf2_and_checks_the_pin():
    stored = auth.hash_pin(PIN, iterations=1000)
    scheme, iterations, salt, digest = stored.split(":")
    assert (scheme, iterations) == ("pbkdf2_sha256", "1000")
    assert len(bytes.fromhex(salt)) == 16 and len(bytes.fromhex(digest)) == 32
    assert "$" not in stored and PIN not in stored
    assert auth.pin_matches(PIN, stored)
    assert not auth.pin_matches(WRONG, stored)
    assert not auth.pin_matches("", stored)
    # A new salt each time: the same PIN never gives the same line.
    assert auth.hash_pin(PIN, iterations=1000) != stored


def test_the_hash_is_standard_pbkdf2():
    import hashlib
    _, _, salt, digest = STORED.split(":")
    expected = hashlib.pbkdf2_hmac("sha256", PIN.encode(), bytes.fromhex(salt), 1000)
    assert expected.hex() == digest


def test_the_default_strength():
    assert auth.PIN_HASH_ITERATIONS == 600_000
    assert auth.hash_pin.__defaults__ == (600_000,)


@pytest.mark.parametrize("bad", [
    "", "482913", "pbkdf2_sha256:1000:zz:00", "md5:1000:" + "00" * 16 + ":" + "00" * 32,
    "pbkdf2_sha256:0:" + "00" * 16 + ":" + "00" * 32,
    "pbkdf2_sha256:1000:" + "00" * 16 + ":" + "00" * 16,
])
def test_a_broken_hash_is_an_error(bad):
    with pytest.raises(auth.PinHashError):
        auth.parse_pin_hash(bad)


@pytest.mark.parametrize("pin, weak", [
    ("000000", True), ("777777", True), ("123456", True), ("012345", True),
    ("654321", True), ("987654", True), ("482913", False), ("112233", False),
    ("135791", False),
])
def test_weak_pins(pin, weak):
    assert (auth.weak_pin_reason(pin) is not None) is weak


# ── Setup command ───────────────────────────────────────────────

def _typed(monkeypatch, *answers):
    replies = iter(answers)
    prompts = []
    monkeypatch.setattr(auth, "getpass", lambda prompt: prompts.append(prompt) or next(replies))
    return prompts


def test_set_pin_prints_the_hash_line(monkeypatch, capsys):
    prompts = _typed(monkeypatch, PIN, PIN)
    assert auth.main(["set-pin"]) == 0
    assert len(prompts) == 2
    out = capsys.readouterr().out
    line = out.strip().splitlines()[-1]
    assert line.startswith("ADMIN_PIN_HASH=pbkdf2_sha256:600000:")
    assert PIN not in out
    assert auth.pin_matches(PIN, line.split("=", 1)[1])


@pytest.mark.parametrize("answers, message", [
    (("12345",), "exactly 6 digits"),
    (("abcdef",), "exactly 6 digits"),
    (("111111",), "too easy to guess"),
    (("123456",), "too easy to guess"),
    ((PIN, WRONG), "differ"),
])
def test_set_pin_refuses(monkeypatch, capsys, answers, message):
    _typed(monkeypatch, *answers)
    assert auth.main(["set-pin"]) == 1
    captured = capsys.readouterr()
    assert "ADMIN_PIN_HASH=" not in captured.out
    assert message in captured.err


def test_set_pin_usage():
    assert auth.main([]) == 2
    assert auth.main(["setup-totp"]) == 2


# ── Sessions ────────────────────────────────────────────────────

def test_the_pin_signs_in_and_sign_out_ends_it(anon):
    resp = anon.post("/api/bot/stop")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Sign in to control the bot"
    assert anon.get("/api/session").status_code == 401

    resp = _login(anon, PIN)
    assert resp.status_code == 200
    body = resp.json()
    token = body["token"]
    assert set(body) == {"token", "expires_at"}
    expires = datetime.fromisoformat(body["expires_at"])
    assert timedelta(hours=167) < expires - datetime.utcnow() <= timedelta(hours=168)

    assert anon.get("/api/session", headers=_bearer(token)).json() == {
        "expires_at": body["expires_at"]
    }
    assert anon.post("/api/bot/stop", headers=_bearer(token)).status_code == 200
    assert anon.post("/api/bot/start", headers=_bearer(token)).status_code == 200
    resp = anon.post(
        "/api/symbols/active", json={"symbol": "NSE:INFY-EQ"}, headers=_bearer(token)
    )
    assert resp.status_code == 200
    assert anon.get("/api/session", headers={"Authorization": f"bearer {token}"}).status_code == 200

    assert anon.post("/api/session/logout", headers=_bearer(token)).json() == {"message": "Signed out"}
    resp = anon.post("/api/bot/stop", headers=_bearer(token))
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Session ended: sign in again"
    assert anon.post("/api/session/logout", headers=_bearer(token)).status_code == 200
    assert anon.post("/api/session/logout").status_code == 200


def test_spaces_in_the_pin_are_fine(anon):
    assert _login(anon, f" {PIN[:3]} {PIN[3:]} ").status_code == 200


def test_a_wrong_or_malformed_pin_is_refused(anon):
    resp = _login(anon, WRONG)
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Wrong PIN"

    for malformed in ("", "12345", "1234567", "abcdef", "١٢٣٤٥٦"):
        resp = _login(anon, malformed)
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Enter your 6-digit PIN"
    assert _login(anon, "1" * 17).status_code == 422
    assert anon.post("/api/session/login", json={"code": PIN}).status_code == 422
    # Not PINs at all: they do not count toward the lockout (1 wrong so far).
    for _ in range(auth.MAX_FAILURES - 1 - 1):
        assert _login(anon, WRONG).status_code == 401
    assert _login(anon, PIN).status_code == 200


def test_a_pin_works_again_and_again(anon):
    first = _sign_in(anon)
    second = _sign_in(anon)
    assert first != second
    for token in (first, second):
        assert anon.get("/api/session", headers=_bearer(token)).status_code == 200


def test_only_a_hash_of_the_token_is_stored(anon):
    token = _sign_in(anon)
    db = sqlite3.connect(settings.DB_PATH)
    assert db.execute("SELECT token_hash FROM sessions").fetchall() == [(auth.token_hash(token),)]
    columns = [row[1] for row in db.execute("PRAGMA table_info(sessions)")]
    assert columns == ["token_hash", "credential", "created_at", "expires_at"]


def test_a_password_era_sessions_table_is_replaced(db_path, monkeypatch, clock):
    db = sqlite3.connect(db_path)
    db.execute(
        "CREATE TABLE sessions (token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, "
        "credential TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL)"
    )
    db.execute("INSERT INTO sessions VALUES ('h', 'admin', 'c', '2026-01-01', '2030-01-01')")
    db.commit()
    db.close()
    monkeypatch.setattr(settings, "ADMIN_PIN_HASH", STORED)
    with TestClient(main.app) as client:
        assert _login(client, PIN).status_code == 200
    columns = [row[1] for row in sqlite3.connect(db_path).execute("PRAGMA table_info(sessions)")]
    assert "username" not in columns


def test_a_made_up_token_is_refused(anon):
    assert anon.post("/api/bot/stop", headers=_bearer("not-a-real-session")).status_code == 401
    assert anon.post("/api/bot/stop", headers={"Authorization": "Basic abc"}).status_code == 401


def test_a_new_pin_ends_every_session(anon, monkeypatch):
    token = _sign_in(anon)
    # Even the same PIN set again: a new salt, so a new hash.
    monkeypatch.setattr(settings, "ADMIN_PIN_HASH", auth.hash_pin(PIN, iterations=1000))
    assert anon.post("/api/bot/stop", headers=_bearer(token)).status_code == 401

    monkeypatch.setattr(settings, "ADMIN_PIN_HASH", STORED)
    token = _sign_in(anon)
    monkeypatch.setattr(settings, "ADMIN_PIN_HASH", "")
    assert anon.get("/api/session", headers=_bearer(token)).status_code == 401


def test_an_expired_session_is_refused_and_removed(anon, monkeypatch):
    real_now = auth._utcnow
    monkeypatch.setattr(auth, "_utcnow", lambda: real_now() - timedelta(hours=169))
    token = _sign_in(anon)
    monkeypatch.setattr(auth, "_utcnow", real_now)

    assert anon.post("/api/bot/stop", headers=_bearer(token)).status_code == 401
    count = sqlite3.connect(settings.DB_PATH).execute("SELECT COUNT(*) FROM sessions").fetchone()
    assert count == (0,)


def test_sign_in_not_set_up(db_path, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_PIN_HASH", "")
    with TestClient(main.app) as client:
        resp = _login(client, PIN)
    assert resp.status_code == 503
    assert "python -m utils.auth set-pin" in resp.json()["detail"]


def test_a_broken_hash_says_so(db_path, monkeypatch):
    # The PIN itself put in .env by mistake.
    monkeypatch.setattr(settings, "ADMIN_PIN_HASH", PIN)
    with TestClient(main.app) as client:
        resp = _login(client, PIN)
    assert resp.status_code == 503
    assert "not a valid hash" in resp.json()["detail"]


# ── Wrong-PIN lockout ───────────────────────────────────────────

def test_five_wrong_pins_lock_sign_in_for_15_minutes(anon, clock):
    for _ in range(auth.MAX_FAILURES):
        assert _login(anon, WRONG).status_code == 401
    # Locked: even the right PIN is not checked.
    resp = _login(anon, PIN)
    assert resp.status_code == 429
    assert resp.json()["detail"] == "Too many wrong PINs: try again in 15 min"
    assert resp.headers["Retry-After"] == "900"

    clock.advance(15 * 60)
    assert _login(anon, PIN).status_code == 200


def test_a_right_pin_resets_the_count(anon):
    for _ in range(auth.MAX_FAILURES - 1):
        _login(anon, WRONG)
    assert _login(anon, PIN).status_code == 200
    for _ in range(auth.MAX_FAILURES - 1):
        _login(anon, WRONG)
    assert _login(anon, PIN).status_code == 200


def test_guesses_sent_together_cannot_pass_the_lock(signin):
    """Many guesses at once are checked one by one, so those after the
    fifth wrong one are refused unchecked."""
    import asyncio

    async def guesses():
        return await asyncio.gather(
            *(auth.login(WRONG, "203.0.113.9") for _ in range(8)), return_exceptions=True
        )

    from database import init_db
    asyncio.run(init_db())
    codes = [exc.status_code for exc in asyncio.run(guesses())]
    assert codes == [401] * auth.MAX_FAILURES + [429] * 3


def test_each_lock_lasts_twice_as_long_up_to_a_day():
    throttle = auth.LoginThrottle(max_failures=2)
    now = 0.0
    locks = []
    for _ in range(9):
        throttle.failed("a", now)
        throttle.failed("a", now)
        wait = throttle.retry_after("a", now)
        locks.append(wait / 60)
        now += wait
    assert locks == [15, 30, 60, 120, 240, 480, 960, 1440, 1440]
    assert throttle.retry_after("b", now) == 0  # other addresses unaffected


def test_the_count_is_forgotten_after_a_quiet_day():
    throttle = auth.LoginThrottle(max_failures=2)
    throttle.failed("a", 0)
    throttle.failed("a", 0)  # locked until 900
    assert throttle.retry_after("a", 900) == 0
    # Still within a day of the lock's end: the next lock doubles.
    throttle.failed("a", 1000)
    throttle.failed("a", 1000)
    assert throttle.retry_after("a", 1000) == 1800
    # A full quiet day after that lock ended: back to the first lock.
    quiet = 1000 + 1800 + 24 * 3600
    throttle.failed("a", quiet)
    throttle.failed("a", quiet)
    assert throttle.retry_after("a", quiet) == 900


def test_a_right_pin_clears_an_address():
    throttle = auth.LoginThrottle(max_failures=2)
    throttle.failed("a", 0)
    throttle.succeeded("a")
    throttle.failed("a", 1)
    assert throttle.retry_after("a", 1) == 0


def test_many_addresses_together_lock_everyone(anon, monkeypatch):
    addresses = iter(f"203.0.113.{i}" for i in range(100))
    monkeypatch.setattr(main, "client_address", lambda request: next(addresses))
    for _ in range(auth.MAX_FAILURES_ALL):
        assert _login(anon, WRONG).status_code == 401
    assert _login(anon, PIN).status_code == 429


@pytest.mark.parametrize("seconds, text", [
    (1, "1 min"), (899, "15 min"), (900, "15 min"), (3599, "1 h"),
    (3600, "1 h"), (5400, "2 h"), (86400, "24 h"),
])
def test_wait_text(seconds, text):
    assert auth._wait_text(seconds) == text


def _request(peer, real_ip=None):
    headers = [(b"x-real-ip", real_ip.encode())] if real_ip else []
    return Request({"type": "http", "client": (peer, 5000), "headers": headers})


def test_real_ip_header_is_trusted_only_from_the_proxy():
    # nginx (a private / loopback peer) passes the caller's address.
    assert auth.client_address(_request("172.18.0.3", "198.51.100.7")) == "198.51.100.7"
    assert auth.client_address(_request("127.0.0.1", "198.51.100.7")) == "198.51.100.7"
    # A caller on the internet cannot pick its own address.
    assert auth.client_address(_request("8.8.4.4", "10.0.0.1")) == "8.8.4.4"
    assert auth.client_address(_request("172.18.0.3")) == "172.18.0.3"


# ── The shared key ──────────────────────────────────────────────

def test_the_admin_key_still_works_for_scripts(client):
    assert client.post("/api/bot/stop").status_code == 200


def test_the_default_admin_key_is_never_accepted(db_path, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_SECRET_KEY", auth.DEFAULT_ADMIN_KEY)
    with TestClient(main.app, headers={"X-Admin-Key": auth.DEFAULT_ADMIN_KEY}) as client:
        assert client.post("/api/bot/stop").status_code == 401
        assert client.post("/mcp/invoke/stop_bot", json={"confirm": True}).status_code == 401
    monkeypatch.setattr(settings, "ADMIN_SECRET_KEY", "")
    with TestClient(main.app, headers={"X-Admin-Key": ""}) as client:
        assert client.post("/api/bot/stop").status_code == 401


def test_mcp_control_accepts_a_session(anon):
    token = _sign_in(anon)
    assert anon.post("/mcp/invoke/start_bot", json={"confirm": True}).status_code == 401
    resp = anon.post(
        "/mcp/invoke/start_bot", json={"confirm": True}, headers=_bearer(token)
    )
    assert resp.status_code == 200
