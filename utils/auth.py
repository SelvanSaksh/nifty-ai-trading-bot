"""
Admin-key guard for state-changing endpoints.

The API is deployed with CORS wide open (``allow_origins=["*"]``) so the
dashboard can reach it from anywhere, and none of the mutating routes ever
checked ``settings.ADMIN_SECRET_KEY`` for anything — meaning anyone who could
reach the deployment could start/stop the trading engine or switch the traded
instrument. This makes that key actually required.

This is a shared-secret check, not per-user auth: a key embedded in a client
build is extractable by anyone who has the build. It is a deterrent against
opportunistic/drive-by calls to a publicly reachable API, not a substitute
for real authentication if this is ever exposed to untrusted users.
"""
from fastapi import Header, HTTPException


def verify_admin_key(provided: str) -> None:
    from config import settings  # imported lazily so tests can monkeypatch settings

    if not settings.ADMIN_SECRET_KEY or provided != settings.ADMIN_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Missing or invalid X-Admin-Key header")


async def require_admin_key(x_admin_key: str = Header(default="")) -> None:
    """FastAPI dependency: raise 401 unless X-Admin-Key matches ADMIN_SECRET_KEY."""
    verify_admin_key(x_admin_key)
