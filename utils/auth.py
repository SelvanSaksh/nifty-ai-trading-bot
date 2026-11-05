"""
Who may control the bot.

State-changing endpoints (bot start/stop/restart, symbol switch, the MCP
server's state-changing tools) accept either:

* a dashboard session, ``Authorization: Bearer <token>``, from
  ``POST /api/session/login`` with the owner's 6-digit PIN, or
* the shared key, ``X-Admin-Key: <ADMIN_SECRET_KEY>``, for scripts and the
  MCP client. The placeholder default key is never accepted.

The server keeps only a salted PBKDF2-SHA256 hash of the PIN,
``ADMIN_PIN_HASH`` (made with ``python -m utils.auth set-pin``). Session
tokens are stored only as SHA-256 hashes. A session ends when it expires
(``SESSION_HOURS``), on sign-out, or when the PIN changes.

Wrong PINs lock sign-in: ``MAX_FAILURES`` from one address, or
``MAX_FAILURES_ALL`` from all addresses together (port 8000 is reachable
without nginx, where the caller's address may not be the real one). Each
lock lasts twice the one before, from ``LOCKOUT_SECONDS`` up to
``MAX_LOCKOUT_SECONDS``, and the count is forgotten after ``FORGET_SECONDS``
without wrong PINs. The lockout is kept in memory: the API runs as one
process, and a restart clears it.
"""
import asyncio
import hashlib
import hmac
import ipaddress
import math
import secrets
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from getpass import getpass
from typing import Dict, Optional

from fastapi import Header, HTTPException, Request

DEFAULT_ADMIN_KEY = "change-me-in-production"

PIN_DIGITS = 6
PIN_HASH_SCHEME = "pbkdf2_sha256"
PIN_HASH_ITERATIONS = 600_000  # OWASP's 2023 figure for PBKDF2-SHA256
PIN_SALT_BYTES = 16

MAX_FAILURES = 5
MAX_FAILURES_ALL = 10
LOCKOUT_SECONDS = 15 * 60
MAX_LOCKOUT_SECONDS = 24 * 60 * 60
FORGET_SECONDS = 24 * 60 * 60


class PinHashError(ValueError):
    """ADMIN_PIN_HASH is not a hash made by ``set-pin``."""


# ── The PIN ─────────────────────────────────────────────────────

def is_pin_format(pin: str) -> bool:
    return len(pin) == PIN_DIGITS and pin.isascii() and pin.isdigit()


def weak_pin_reason(pin: str) -> Optional[str]:
    """Why [pin] is too easy to guess (one digit repeated, or a straight
    run like 123456 / 654321), or None."""
    if len(set(pin)) == 1:
        return "it is one digit repeated"
    steps = {int(b) - int(a) for a, b in zip(pin, pin[1:])}
    if steps in ({1}, {-1}):
        return "it is a straight run of digits"
    return None


def _derive(pin: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", pin.encode("ascii"), salt, iterations)


def hash_pin(pin: str, iterations: int = PIN_HASH_ITERATIONS) -> str:
    """``pbkdf2_sha256:<iterations>:<salt hex>:<hash hex>``. Colons, not
    ``$``: docker compose would read ``$`` in .env as a variable."""
    salt = secrets.token_bytes(PIN_SALT_BYTES)
    digest = _derive(pin, salt, iterations)
    return f"{PIN_HASH_SCHEME}:{iterations}:{salt.hex()}:{digest.hex()}"


def parse_pin_hash(stored: str):
    """(iterations, salt, digest) of a stored hash; PinHashError if broken."""
    parts = stored.strip().split(":")
    if len(parts) != 4 or parts[0] != PIN_HASH_SCHEME:
        raise PinHashError("not a pbkdf2_sha256 hash")
    try:
        iterations = int(parts[1])
        salt = bytes.fromhex(parts[2])
        digest = bytes.fromhex(parts[3])
    except ValueError as exc:
        raise PinHashError(str(exc)) from exc
    if iterations < 1 or len(salt) < 8 or len(digest) != 32:
        raise PinHashError("bad iterations, salt or hash length")
    return iterations, salt, digest


def pin_matches(pin: str, stored: str) -> bool:
    iterations, salt, digest = parse_pin_hash(stored)
    if not is_pin_format(pin):
        return False
    return hmac.compare_digest(_derive(pin, salt, iterations), digest)


def credential_fingerprint(stored: str) -> str:
    """Stored with each session: a new PIN (so a new salted hash) changes
    it, which ends every older session."""
    return hashlib.sha256(f"pin\n{stored.strip()}".encode("utf-8")).hexdigest()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ── Wrong-PIN lockout ───────────────────────────────────────────

@dataclass
class _Record:
    failures: int = 0  # since the last lock
    locks: int = 0
    locked_until: float = 0.0
    last_failure: float = 0.0


class LoginThrottle:
    """Wrong PINs per key (a client address). Every [max_failures] wrong
    PINs lock that key, each lock twice as long as the one before, from
    [base] up to [cap] seconds. A key with no wrong PIN for [forget]
    seconds after its last lock ended starts over."""

    def __init__(
        self,
        max_failures: int = MAX_FAILURES,
        base: float = LOCKOUT_SECONDS,
        cap: float = MAX_LOCKOUT_SECONDS,
        forget: float = FORGET_SECONDS,
    ):
        self.max_failures = max_failures
        self.base = base
        self.cap = cap
        self.forget = forget
        self._records: Dict[str, _Record] = {}

    def _record(self, key: str, now: float) -> Optional[_Record]:
        record = self._records.get(key)
        if record is None:
            return None
        quiet_since = max(record.last_failure, record.locked_until)
        if now >= record.locked_until and now - quiet_since >= self.forget:
            del self._records[key]
            return None
        return record

    def retry_after(self, key: str, now: float) -> float:
        """Seconds until [key] may try again; 0 if it may now."""
        record = self._record(key, now)
        return max(0.0, record.locked_until - now) if record else 0.0

    def failed(self, key: str, now: float) -> None:
        record = self._record(key, now)
        if record is None:
            record = self._records[key] = _Record()
        record.failures += 1
        record.last_failure = now
        if record.failures >= self.max_failures:
            record.locks += 1
            record.failures = 0
            record.locked_until = now + min(self.cap, self.base * 2 ** (record.locks - 1))

    def succeeded(self, key: str) -> None:
        self._records.pop(key, None)

    def clear(self) -> None:
        self._records.clear()


login_throttle = LoginThrottle()
# Every address together, under one key. A right PIN does not reset it.
global_login_throttle = LoginThrottle(max_failures=MAX_FAILURES_ALL)
ALL_CLIENTS = "*"


def _wait_text(seconds: float) -> str:
    minutes = math.ceil(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    return f"{math.ceil(seconds / 3600)} h"


def client_address(request: Request) -> str:
    """The caller's address. Behind nginx every request comes from a private
    address with the real one in X-Real-IP, which is trusted only from such
    a peer (a caller on the internet cannot set it for itself)."""
    peer = request.client.host if request.client else ""
    forwarded = request.headers.get("x-real-ip", "").strip()
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return peer or "unknown"
    if forwarded and (address.is_private or address.is_loopback):
        return forwarded
    return peer


# ── Sessions ────────────────────────────────────────────────────

# One PIN check at a time: many guesses sent together cannot all be checked
# before the first wrong ones lock sign-in.
_pin_lock = asyncio.Lock()


def _utcnow() -> datetime:
    return datetime.utcnow().replace(microsecond=0)


async def login(pin: str, client: str) -> Dict[str, str]:
    """A new session for the right PIN; raises 400 if [pin] is not 6 digits,
    401 if it is wrong, 429 while locked out, 503 if sign-in is not set up."""
    from config import settings  # read at call time: tests patch settings
    from database import create_session

    stored = settings.ADMIN_PIN_HASH.strip()
    if not stored:
        raise HTTPException(
            status_code=503,
            detail="Sign-in is not set up: run python -m utils.auth set-pin and put "
            "ADMIN_PIN_HASH in the server's .env",
        )
    try:
        parse_pin_hash(stored)
    except PinHashError:
        raise HTTPException(
            status_code=503,
            detail="ADMIN_PIN_HASH is not a valid hash: make a new one with "
            "python -m utils.auth set-pin",
        )

    pin = pin.replace(" ", "")
    if not is_pin_format(pin):
        raise HTTPException(status_code=400, detail="Enter your 6-digit PIN")

    async with _pin_lock:
        now = time.monotonic()
        wait = max(
            login_throttle.retry_after(client, now),
            global_login_throttle.retry_after(ALL_CLIENTS, now),
        )
        if wait > 0:
            raise HTTPException(
                status_code=429,
                detail=f"Too many wrong PINs: try again in {_wait_text(wait)}",
                headers={"Retry-After": str(math.ceil(wait))},
            )
        # PBKDF2 takes a few hundred ms: off the event loop.
        if not await asyncio.to_thread(pin_matches, pin, stored):
            login_throttle.failed(client, now)
            global_login_throttle.failed(ALL_CLIENTS, now)
            raise HTTPException(status_code=401, detail="Wrong PIN")

    login_throttle.succeeded(client)
    token = secrets.token_urlsafe(32)
    created = _utcnow()
    expires = created + timedelta(hours=max(1, settings.SESSION_HOURS))
    await create_session(token_hash(token), credential_fingerprint(stored), created, expires)
    return {"token": token, "expires_at": expires.isoformat()}


async def session_for(token: str) -> Optional[Dict[str, str]]:
    """The live session of [token] (its expiry). None, and the row removed,
    when it is unknown, expired, or made with an older PIN."""
    from config import settings
    from database import delete_session, get_session

    if not token:
        return None
    key = token_hash(token)
    row = await get_session(key)
    if row is None:
        return None
    stored = settings.ADMIN_PIN_HASH.strip()
    current = bool(stored) and hmac.compare_digest(row["credential"], credential_fingerprint(stored))
    if not current or datetime.fromisoformat(row["expires_at"]) <= datetime.utcnow():
        await delete_session(key)
        return None
    return {"expires_at": row["expires_at"]}


async def logout(token: str) -> None:
    from database import delete_session

    if token:
        await delete_session(token_hash(token))


def bearer_token(authorization: str) -> str:
    scheme, _, token = authorization.strip().partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def admin_key_ok(provided: str) -> bool:
    from config import settings

    key = settings.ADMIN_SECRET_KEY
    if not key or key == DEFAULT_ADMIN_KEY or not provided:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), key.encode("utf-8"))


async def verify_admin(authorization: str = "", x_admin_key: str = "") -> None:
    """Raises 401 unless the caller is signed in or sent the admin key."""
    if admin_key_ok(x_admin_key):
        return
    token = bearer_token(authorization)
    if token and await session_for(token) is not None:
        return
    raise HTTPException(
        status_code=401,
        detail="Session ended: sign in again" if token else "Sign in to control the bot",
    )


async def require_admin(
    authorization: str = Header(default=""),
    x_admin_key: str = Header(default=""),
) -> None:
    """FastAPI dependency for state-changing endpoints (see the module doc)."""
    await verify_admin(authorization, x_admin_key)


# ── Setup: python -m utils.auth set-pin ─────────────────────────

def set_pin() -> int:
    """Asks for a PIN twice (not shown as it is typed) and prints the
    ADMIN_PIN_HASH line for the server's .env."""
    pin = getpass("Choose a 6-digit PIN (it is not shown as you type): ").strip()
    if not is_pin_format(pin):
        print("A PIN is exactly 6 digits (0-9). Run this again.", file=sys.stderr)
        return 1
    reason = weak_pin_reason(pin)
    if reason:
        print(f"That PIN is too easy to guess: {reason}. Run this again.", file=sys.stderr)
        return 1
    if getpass("Type it again: ").strip() != pin:
        print("The two PINs differ. Run this again.", file=sys.stderr)
        return 1
    print("Put this line in the server's .env (replacing any older one) and restart the API:")
    print(f"ADMIN_PIN_HASH={hash_pin(pin)}")
    return 0


def main(argv) -> int:
    if list(argv) != ["set-pin"]:
        print("usage: python -m utils.auth set-pin", file=sys.stderr)
        return 2
    return set_pin()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
