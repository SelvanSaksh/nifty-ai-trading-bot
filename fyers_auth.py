#!/usr/bin/env python3
"""
Fyers Authentication — Generates access token valid for 15 days.
Uses official Fyers API endpoints.
Run: python fyers_auth.py
"""
import os
import sys
import webbrowser
import asyncio
import aiohttp
import hashlib
from urllib.parse import parse_qs, urlparse
from datetime import datetime, timedelta
from typing import Optional, Tuple

from config import settings



FYERS_TOKEN_FILE = "data/fyers_token.txt"
FYERS_API_URL = "https://api-t1.fyers.in/api/v3"


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
    if not settings.FYERS_APP_ID or not settings.FYERS_SECRET:
        print("❌ Error: FYERS_APP_ID and FYERS_SECRET must be set in .env")
        sys.exit(1)
    
    # client_id must be the full APP_ID with -100 suffix
    auth_url = (
        f"{FYERS_API_URL}/generate-authcode"
        f"?client_id={settings.FYERS_APP_ID}"
        f"&redirect_uri={settings.FYERS_REDIRECT_URI}"
        f"&response_type=code"
        f"&state=niftybot"
    )
    
    return auth_url


async def exchange_code_for_token(auth_code: str) -> str:
    """Exchange authorization code for access token."""
    # Generate appIdHash: SHA256 of app_id:secret (full app_id with -100)
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
    
    # Build full token with app_id prefix (WITH -100)
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