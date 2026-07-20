"""
Production token management with auto-refresh.
No manual intervention needed.
"""
import os
import json
import asyncio
import aiohttp
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

from config import settings


TOKEN_FILE = "/app/data/fyers_tokens.json"


def load_tokens() -> Dict[str, Any]:
    """Load tokens from persistent volume."""
    if not os.path.exists(TOKEN_FILE):
        return {}
    try:
        with open(TOKEN_FILE, "r") as f:
            return json.load(f)
    except:
        return {}


def save_tokens(access_token: str, refresh_token: Optional[str] = None):
    """Save tokens to persistent volume."""
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    data = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "updated_at": datetime.now().isoformat(),
        "expires_at": (datetime.now() + timedelta(days=14)).isoformat()  # 14 days to be safe
    }
    with open(TOKEN_FILE, "w") as f:
        json.dump(data, f, indent=2)
    print(f"[TOKEN] Saved to {TOKEN_FILE}")


async def validate_token(token: str) -> bool:
    """Check if token is still valid."""
    headers = {"Authorization": token}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api-t1.fyers.in/api/v3/profile",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()
                return data.get("s") == "ok"
    except Exception as e:
        print(f"[TOKEN] Validation error: {e}")
        return False


async def refresh_access_token(refresh_token: str) -> Optional[str]:
    """Get new access token using refresh token."""
    payload = {
        "grant_type": "refresh_token",
        "appId": settings.FYERS_APP_ID,
        "secretId": settings.FYERS_SECRET,
        "refresh_token": refresh_token
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api-t1.fyers.in/api/v3/token",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                data = await resp.json()
                
                if data.get("s") == "ok" and "access_token" in data:
                    return data["access_token"]
                
                print(f"[TOKEN] Refresh failed: {data}")
                return None
    except Exception as e:
        print(f"[TOKEN] Refresh error: {e}")
        return None


async def auto_refresh_token() -> str:
    """
    Main entry point: ensures valid token exists.
    Called by cron job or on startup.
    """
    tokens = load_tokens()
    
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    expires_at_str = tokens.get("expires_at")
    
    # Check existing token
    if access_token:
        print("[TOKEN] Validating existing token...")
        if await validate_token(access_token):
            print("[TOKEN] Token valid")
            return access_token
        print("[TOKEN] Token invalid or expired")
    
    # Try refresh
    if refresh_token:
        print("[TOKEN] Attempting refresh...")
        new_token = await refresh_access_token(refresh_token)
        if new_token:
            full_token = f"{settings.FYERS_APP_ID}:{new_token}"
            save_tokens(full_token, refresh_token)
            return full_token
    
    # Cannot auto-refresh — alert admin
    print("[TOKEN] CRITICAL: Cannot refresh token. Manual re-auth required!")
    
    # Write alert file for monitoring
    alert_file = "/app/data/TOKEN_EXPIRED_ALERT"
    with open(alert_file, "w") as f:
        f.write(f"Token expired at {datetime.now().isoformat()}\n")
        f.write("Manual re-authentication required:\n")
        f.write("1. Run: python fyers_auth.py\n")
        f.write("2. Update .env with new token\n")
        f.write("3. Restart container: docker-compose restart bot\n")
    
    raise Exception("Token expired and refresh failed. Manual re-auth required.")


async def update_env_token():
    """Update environment variable with current token (for container restarts)."""
    token = await auto_refresh_token()
    os.environ["FYERS_ACCESS_TOKEN"] = token
    return token


# Backwards compatibility
if __name__ == "__main__":
    asyncio.run(auto_refresh_token())