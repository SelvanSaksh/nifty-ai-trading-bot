"""
Fyers Authentication — Generates access token valid for 15 days.
Uses official Fyers API endpoints.
Run: python fyers_auth.py
"""
import os
import sys
import time
import webbrowser
import asyncio
import aiohttp
import hashlib
from urllib.parse import parse_qs, urlparse, quote
from datetime import datetime, timedelta
from typing import Optional, Tuple

from config import settings



FYERS_TOKEN_FILE = "data/fyers_token.txt"
FYERS_API_URL = settings.FYERS_API_URL

# How long a definitive (valid/invalid) /profile answer may be reused. The web
# client polls /api/auth/status every few seconds, so the probe must be cached.
PROBE_TTL_SECONDS = 45.0
# Transient failures (Fyers unreachable, timeout) are retried sooner.
PROBE_RETRY_SECONDS = 10.0

# Hostnames a callback may only ever serve when the API itself runs there.
LOOPBACK_HOSTS = frozenset({"", "localhost", "127.0.0.1", "::1", "0.0.0.0"})


def _belongs_to_current_app(token: str) -> bool:
    """Reject tokens minted for a different Fyers app (stale credentials)."""
    if not token or ":" not in token:
        return True
    if not settings.FYERS_APP_ID:
        return True
    return token.split(":", 1)[0] == settings.FYERS_APP_ID


def save_token(token: str):
    """Save token to file with timestamp."""
    os.makedirs("data", exist_ok=True)
    with open(FYERS_TOKEN_FILE, "w") as f:
        f.write(f"{token}\n")
        f.write(f"generated_at:{datetime.now().isoformat()}\n")
        f.write(f"expires_at:{(datetime.now() + timedelta(days=15)).isoformat()}\n")
    print(f"✅ Token saved to {FYERS_TOKEN_FILE}")


def load_token() -> Tuple[str, Optional[datetime]]:
    """Load token and check expiry."""
    if not os.path.exists(FYERS_TOKEN_FILE):
        return "", None
    
    with open(FYERS_TOKEN_FILE, "r") as f:
        lines = f.read().strip().split("\n")
    
    token = lines[0].strip()
    expires_at = None

    if not _belongs_to_current_app(token):
        # Minted for another app id — unusable against the configured app.
        return "", None

    for line in lines[1:]:
        if line.startswith("expires_at:"):
            try:
                expires_at = datetime.fromisoformat(line.replace("expires_at:", ""))
            except:
                pass
    
    return token, expires_at


def is_token_valid(token: str, expires_at: Optional[datetime]) -> bool:
    """Check if token is still valid."""
    if not token or not expires_at:
        return False
    return datetime.now() < (expires_at - timedelta(days=1))


def active_token() -> Tuple[str, Optional[datetime], Optional[str]]:
    """The token every outbound Fyers call uses.

    A token minted by a completed OAuth login lives in the token file (which
    survives restarts) and is newer than whatever the process was configured
    with, so it wins while it is still valid. The environment value is the
    bootstrap used before the first login — it cannot be rotated without a
    restart, so it must never shadow a fresh one.
    """
    file_token, file_expires = load_token()
    if file_token and is_token_valid(file_token, file_expires):
        return file_token, file_expires, "file"
    env_token = settings.FYERS_ACCESS_TOKEN
    if env_token:
        return env_token, None, "environment"
    return file_token, file_expires, ("file" if file_token else None)


# Cached answer of "does Fyers still accept this token?": None = unknown.
_probe: dict = {"token": None, "valid": None, "checked_at": 0.0}


def reset_token_probe() -> None:
    """Forget the cached verdict (a fresh token was just stored)."""
    _probe.update(token=None, valid=None, checked_at=0.0)


def mark_token_rejected() -> None:
    """Fyers refused a request with the current token — report it at once."""
    token, _, _ = active_token()
    _probe.update(token=token, valid=False, checked_at=time.monotonic())


async def verify_token(token: str, force: bool = False) -> Optional[bool]:
    """Ask Fyers whether it still accepts ``token``.

    Returns True (accepted), False (rejected — expired or revoked) or None when
    the answer could not be determined (network/5xx), so a Fyers outage never
    makes the app bounce users into a pointless re-login.
    """
    if not token:
        return False

    now = time.monotonic()
    if _probe["token"] == token:
        ttl = PROBE_TTL_SECONDS if _probe["valid"] is not None else PROBE_RETRY_SECONDS
        if not force and now - _probe["checked_at"] < ttl:
            return _probe["valid"]

    verdict: Optional[bool] = None
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                f"{FYERS_API_URL}/profile", headers={"Authorization": token}
            ) as resp:
                if resp.status == 401:
                    verdict = False
                elif resp.status == 200:
                    data = await resp.json(content_type=None)
                    verdict = data.get("s") == "ok"
    except Exception:
        verdict = None

    _probe.update(token=token, valid=verdict, checked_at=now)
    return verdict


async def auth_status() -> dict:
    """Return safe token state for the web client; never expose the token.

    The token is *verified* against Fyers, not merely reported as present: a
    token that Fyers no longer accepts must read as unauthenticated so clients
    can send the user back to the login flow.
    """
    login_available = bool(settings.FYERS_APP_ID and settings.FYERS_SECRET)
    token, expires_at, source = active_token()

    if not token:
        return {
            "authenticated": False,
            "source": source,
            "expires_at": None,
            "reason": "Fyers token missing",
            "login_available": login_available,
        }

    verified = await verify_token(token)
    if verified is False:
        return {
            "authenticated": False,
            "source": source,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "reason": "Fyers access token expired",
            "login_available": login_available,
        }
    if verified is True:
        return {
            "authenticated": True,
            "source": source,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "reason": None,
            "login_available": login_available,
        }

    # Fyers could not be reached: trust the recorded expiry when we have one,
    # otherwise stay optimistic rather than interrupting a live session.
    valid = is_token_valid(token, expires_at) if expires_at else True
    return {
        "authenticated": valid,
        "source": source,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "reason": None if valid else "Fyers token expired",
        "login_available": login_available,
    }


def safe_return_url(candidate: Optional[str]) -> Optional[str]:
    """Post-login destination, restricted to origins we own (no open redirect)."""
    if not candidate:
        return None
    try:
        parts = urlparse(candidate)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    host = (parts.hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        return candidate
    allowed = {urlparse(settings.FYERS_REDIRECT_URI).hostname or ""}
    if settings.FRONTEND_URL:
        allowed.add(urlparse(settings.FRONTEND_URL).hostname or "")
    return candidate if host in allowed else None


def effective_redirect_uri(incoming_origin: str, configured: Optional[str] = None) -> str:
    """The callback Fyers must return the browser to for this request.

    ``FYERS_REDIRECT_URI`` defaults to the deployed API, but a local override
    (``http://127.0.0.1:8000/api/auth/callback``) is only reachable from the
    machine running the API. Handing that value to Fyers from the public
    deployment would bounce every user's browser to their own ``127.0.0.1``,
    so when the configured callback is loopback and the request did not arrive
    over loopback, the callback is rebuilt from the origin the client used.
    """
    if configured is None:
        configured = settings.FYERS_REDIRECT_URI
    wanted = urlparse(configured)
    if (wanted.hostname or "").lower() not in LOOPBACK_HOSTS:
        return configured
    incoming = urlparse(incoming_origin)
    if (incoming.hostname or "").lower() in LOOPBACK_HOSTS:
        return configured
    path = wanted.path or "/api/auth/callback"
    return f"{incoming.scheme}://{incoming.netloc}{path}"


def get_auth_url(return_to: Optional[str] = None, redirect_uri: Optional[str] = None) -> str:
    """Build the OAuth login URL for both the API redirect and CLI helper."""
    if not settings.FYERS_APP_ID or not settings.FYERS_SECRET:
        raise ValueError("FYERS_APP_ID and FYERS_SECRET must be configured")
    # Fyers echoes `state` back to the callback, so it carries the page the
    # browser came from and the user lands back on the terminal after login.
    target = safe_return_url(return_to)
    state = quote(target, safe="") if target else "niftybot"
    callback = redirect_uri if redirect_uri is not None else settings.FYERS_REDIRECT_URI
    return (
        f"{FYERS_API_URL}/generate-authcode"
        f"?client_id={settings.FYERS_APP_ID}"
        f"&redirect_uri={quote(callback, safe='')}"
        f"&response_type=code"
        f"&state={state}"
    )


async def validate_token(token: str) -> bool:
    """Validate token by fetching profile via HTTP."""
    headers = {"Authorization": token}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{FYERS_API_URL}/profile", headers=headers) as resp:
                data = await resp.json()
                return data.get("s") == "ok"
    except Exception as e:
        print(f"❌ Validation failed: {e}")
        return False


def generate_auth_url() -> str:
    """Generate Fyers login URL."""
    try:
        return get_auth_url()
    except ValueError as exc:
        print(f"❌ Error: {exc}")
        sys.exit(1)


async def exchange_code_for_token(auth_code: str) -> str:
    """Exchange authorization code for access token."""
    # Generate appIdHash: SHA256 of "app_id:secret" (full app id incl. -200 suffix)
    app_id_hash = hashlib.sha256(f"{settings.FYERS_APP_ID}:{settings.FYERS_SECRET}".encode()).hexdigest()
    
    # ✅ Define payload BEFORE using it
    payload = {
        "grant_type": "authorization_code",
        "appIdHash": app_id_hash,
        "code": auth_code
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{FYERS_API_URL}/validate-authcode",
            json=payload,
            headers={"Content-Type": "application/json"}
        ) as resp:
            data = await resp.json()
            print(f"Token response: {data}")
            
            if data.get("s") != "ok":
                print(f"❌ Token generation failed: {data}")
                sys.exit(1)
            
            return data["access_token"]


async def main():
    """Main authentication flow."""
    print("=" * 65)
    print("Fyers Authentication — Nifty 50 AI Trading Bot")
    print("=" * 65)
    print(f"\nRedirect URL: {settings.FYERS_REDIRECT_URI}")
    print("Set this EXACT URL in your Fyers app dashboard!")
    
    # Check existing token
    existing_token, expires_at = load_token()
    
    if existing_token and is_token_valid(existing_token, expires_at):
        print(f"\n✅ Existing token until {expires_at.strftime('%Y-%m-%d %H:%M')}")
        
        print("\nValidating with Fyers API...")
        if await validate_token(existing_token):
            print("✅ Token is active!")
            print(f"\nFYERS_ACCESS_TOKEN={existing_token}")
            return
        else:
            print("⚠️ Token invalid. Generating new one...")
    
    # Generate new token
    print("\n🔐 Step 1: Opening Fyers login page...")
    auth_url = generate_auth_url()
    print(f"URL: {auth_url}")
    
    try:
        webbrowser.open(auth_url)
        print("Browser opened.")
    except:
        print("Open URL manually.")
    
    print("\n🔐 Step 2: After login, copy the FULL redirect URL or just the auth_code:")
    user_input = input("Paste here: ").strip()
    
    # Extract auth code
    auth_code = None
    if "auth_code=" in user_input:
        parsed = urlparse(user_input)
        params = parse_qs(parsed.query)
        auth_code = params.get("auth_code", [None])[0]
    elif "code=" in user_input:
        parsed = urlparse(user_input)
        params = parse_qs(parsed.query)
        auth_code = params.get("code", [None])[0]
    else:
        auth_code = user_input.strip()
    
    if not auth_code or len(auth_code) < 20:
        print("❌ Invalid auth_code")
        sys.exit(1)
    
    print(f"\n✅ Auth code: {auth_code[:20]}...")
    
    print("\n🔐 Step 3: Exchanging for access token...")
    access_token = await exchange_code_for_token(auth_code)
    
    # Build full token with the app id prefix (e.g. 77M2C2QWOQ-200:<jwt>)
    full_token = f"{settings.FYERS_APP_ID}:{access_token}"
    
    print("\n🔐 Step 4: Validating...")
    if not await validate_token(full_token):
        print("❌ Validation failed!")
        sys.exit(1)
    
    save_token(full_token)
    
    print("\n" + "=" * 65)
    print("✅ Authentication successful!")
    print(f"Token: {full_token[:50]}...{full_token[-10:]}")
    print(f"\nAdd to .env:")
    print(f"FYERS_ACCESS_TOKEN={full_token}")
    print("=" * 65)


if __name__ == "__main__":
    asyncio.run(main())
