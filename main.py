from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional
from urllib.parse import unquote
from pydantic import BaseModel, Field

from bot import NiftyBot, SymbolConflictError, SymbolValidationError
from database import init_db, get_active_symbol_state, get_symbol_change_history
from trading_mcp.server import mcp_app, set_bot_instance
from config import settings, ensure_data_dir
from fyers_auth import (
    auth_status,
    effective_redirect_uri,
    exchange_code_for_token,
    get_auth_url,
    reset_token_probe,
    safe_return_url,
    save_token,
)
from features.watchlist import watchlist_manager
from utils.engine_lock import EngineLockedError


# Headers that let a client verify *which* instrument it is looking at.
CANDLE_RESPONSE_HEADERS = (
    "X-Symbol,X-Symbol-Name,X-Active-Symbol,X-Symbol-Version,X-Timeframe,X-Data-Source"
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage bot lifecycle: start on startup, stop on shutdown."""
    # Startup
    ensure_data_dir()
    await init_db()

    app.state.bot = NiftyBot()
    app.state.startup_error = None
    set_bot_instance(app.state.bot)  # Link to MCP server

    try:
        await app.state.bot.start()
    except EngineLockedError as exc:
        # Another worker/replica owns the engine — serve the API from the
        # shared state instead of running a second trading engine.
        app.state.startup_error = str(exc)
        print(f"[BOOT] {exc}")
        print("[BOOT] API-only mode: this process will not trade.")
    except Exception as exc:
        app.state.startup_error = str(exc)
        print(f"[BOOT] Bot failed to start: {exc}")
        print("[BOOT] API-only mode: use POST /api/bot/start to retry.")

    yield

    # Shutdown
    try:
        await app.state.bot.stop()
    except Exception as exc:
        print(f"[BOOT] Error while stopping bot: {exc}")


app = FastAPI(
    title="Nifty 50 AI Trading Bot",
    description="Full-stack autonomous trading system for Nifty 50 index",
    version="1.0.0",
    lifespan=lifespan
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=CANDLE_RESPONSE_HEADERS.split(","),
)


# ── Health & Root ───────────────────────────────────────────────

@app.get("/")
async def root():
    return {
        "status": "Nifty 50 AI Bot running",
        "mode": "paper" if settings.PAPER_TRADING else "live",
        "version": "1.0.0"
    }


@app.get("/health")
async def health_check():
    bot: NiftyBot = app.state.bot
    active = await get_active_symbol_state()
    return {
        "status": "healthy",
        "bot_running": bot.is_running,
        "engine_leader": bot.engine_leader,
        "symbol": active["symbol"],
        "symbol_version": active["version"],
        "uptime": bot.uptime,
        "websocket_clients": len(bot.ws_clients),
        "startup_error": getattr(app.state, "startup_error", None),
    }


@app.get("/api/auth/status")
async def get_auth_status():
    """Safe Fyers session state for clients that need to reconnect."""
    return await auth_status()


def request_origin(request: Request) -> str:
    """Origin as the *browser* reached it.

    nginx terminates TLS and passes ``X-Forwarded-Proto``/``Host``, but uvicorn
    is not run with ``--proxy-headers``, so ``request.url`` alone would report
    ``http://`` and lose the public name entirely.
    """
    scheme = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    host = request.headers.get("host", "").split(",")[0].strip()
    return f"{scheme or request.url.scheme}://{host or request.url.netloc}"


@app.get("/api/auth/login")
async def begin_fyers_login(request: Request, return_to: Optional[str] = None):
    """Send the browser to Fyers when a token is missing or expired.

    ``return_to`` is the page the client wants back after login; it travels
    through OAuth ``state`` and is only honoured for origins we own.
    """
    redirect_uri = effective_redirect_uri(request_origin(request))
    if redirect_uri != settings.FYERS_REDIRECT_URI:
        print(f"[AUTH] loopback FYERS_REDIRECT_URI overridden -> {redirect_uri}")
    try:
        return RedirectResponse(get_auth_url(return_to, redirect_uri), status_code=307)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


# ── Fyers Auth Callback ─────────────────────────────────────────

@app.get("/api/auth/callback")
async def fyers_auth_callback(request: Request):
    """Handle Fyers OAuth callback — exchanges auth_code for access token."""
    params = dict(request.query_params)
    auth_code = params.get("auth_code") or params.get("code")

    if not auth_code:
        return HTMLResponse(
            "<h2>Auth Failed</h2><p>No auth_code in callback.</p>",
            status_code=400,
        )

    try:
        access_token = await exchange_code_for_token(auth_code)
        full_token = f"{settings.FYERS_APP_ID}:{access_token}"
        save_token(full_token)

        # Reload settings so the running app picks up the new token
        settings.FYERS_ACCESS_TOKEN = full_token
        # The cached "is this token valid?" verdict is about the old token, and
        # the broker's HTTP session/socket still carry the old credentials.
        reset_token_probe()
        bot = getattr(app.state, "bot", None)
        if bot is not None:
            try:
                await bot.reauthenticate()
            except Exception as exc:
                print(f"[AUTH] Failed to re-arm the broker with the new token: {exc}")

        # `state` travels through Fyers, so it may come back decoded (normal) or
        # still percent-encoded; accept either, but never a foreign origin.
        raw_state = params.get("state", "") or ""
        return_url = safe_return_url(raw_state) or safe_return_url(unquote(raw_state))
        if return_url:
            return RedirectResponse(return_url, status_code=302)

        return HTMLResponse(
            "<h2>Auth Successful</h2>"
            "<p>Token saved. You can close this tab and reopen the terminal.</p>",
            status_code=200,
        )
    except Exception as e:
        return HTMLResponse(
            f"<h2>Token Exchange Failed</h2><p>{e}</p>",
            status_code=500,
        )


# ── Bot Status ──────────────────────────────────────────────────

@app.get("/api/status")
async def get_status():
    bot: NiftyBot = app.state.bot
    active = await get_active_symbol_state()
    return {
        "running": bot.is_running,
        "uptime": bot.uptime,
        "active_trade": bot.active_trade.model_dump() if bot.active_trade else None,
        "paper_mode": settings.PAPER_TRADING,
        "today_pnl": round(bot.today_pnl, 2),
        "symbol": active["symbol"],
        "symbol_name": active["symbol_name"],
        "symbol_version": active["version"],
        "symbol_changed_at": active["changed_at"],
        "symbol_changed_by": active["changed_by"],
        "engine_leader": bot.engine_leader,
        "symbol_in_sync": (not bot.is_running) or (
            bot.symbol == active["symbol"] and bot.symbol_version == active["version"]
        ),
    }


@app.get("/api/stats")
async def get_stats():
    return await app.state.bot.get_trading_stats()


# ── Trades ──────────────────────────────────────────────────────

@app.get("/api/trades")
async def get_recent_trades(limit: int = 20):
    return await app.state.bot.get_recent_trades(limit)


@app.get("/api/trades/{trade_id}")
async def get_trade(trade_id: str):
    trades = await app.state.bot.get_recent_trades(1000)
    for trade in trades:
        if trade.id == trade_id:
            return trade.model_dump()
    return {"error": "Trade not found"}


# ── Signals ─────────────────────────────────────────────────────

@app.get("/api/signals")
async def get_recent_signals(limit: int = 20):
    return await app.state.bot.get_recent_signals(limit)


# ── Candles ─────────────────────────────────────────────────────

def _header_value(value) -> str:
    """HTTP header values must be ASCII — escape anything exotic."""
    return str(value).encode("ascii", "backslashreplace").decode("ascii")


def _candle_headers(result: dict) -> dict:
    return {
        "X-Symbol": _header_value(result["symbol"]),
        "X-Symbol-Name": _header_value(result["symbol_name"]),
        "X-Active-Symbol": _header_value(result["active_symbol"]),
        "X-Symbol-Version": _header_value(result["symbol_version"]),
        "X-Timeframe": _header_value(result["timeframe"]),
        "X-Data-Source": _header_value(result["source"]),
    }


async def _load_candles(
    timeframe: str, limit: int, symbol: str, end_time: Optional[str]
) -> dict:
    bot: NiftyBot = app.state.bot
    tf = (timeframe or "").strip()
    value = (symbol or "").strip()
    if not value:
        raise HTTPException(status_code=422, detail="symbol is required")
    if not tf:
        raise HTTPException(status_code=422, detail="timeframe is required")
    try:
        return await bot.get_candles(
            timeframe=tf, limit=limit, symbol=value, end_time=end_time
        )
    except ValueError as exc:  # SymbolValidationError subclasses ValueError
        raise HTTPException(status_code=422, detail=str(exc))


@app.get("/api/candles")
async def get_candles(
    response: Response,
    symbol: str,
    timeframe: str,
    limit: int = 100,
    end_time: Optional[str] = None,
):
    """
    Candles for ``symbol`` on ``timeframe`` — both supplied by the client.

    Neither the instrument nor the interval is inferred server-side: the FE
    decides what it renders, and every candle carries
    ``symbol``/``symbol_name``/``timeframe`` plus response headers so a client
    can detect a change instead of silently rendering mismatched data.
    """
    result = await _load_candles(timeframe, limit, symbol, end_time)
    for key, value in _candle_headers(result).items():
        response.headers[key] = value
    return result["candles"]


@app.get("/api/candles/detail")
async def get_candles_detail(
    symbol: str,
    timeframe: str,
    limit: int = 100,
    end_time: Optional[str] = None,
):
    """Same data as /api/candles plus the full envelope (coverage, source, error)."""
    return await _load_candles(timeframe, limit, symbol, end_time)


# ── Market Context ──────────────────────────────────────────────

@app.get("/api/market-context")
async def get_market_context():
    return await app.state.bot.get_market_context()


# ── Symbol Management ───────────────────────────────────────

@app.get("/api/symbols")
async def get_available_symbols():
    """Return the list of available instruments for trading."""
    default_wl = watchlist_manager.create_default("default")
    return {
        "symbols": [item.to_dict() for item in default_wl.items]
    }


class SetActiveSymbolRequest(BaseModel):
    symbol: str
    symbol_name: str = ""
    changed_by: str = Field(default="api", max_length=64)


@app.get("/api/symbols/active")
async def get_active_symbol():
    """
    The active trading symbol, read from the shared state.

    Every process answers from the same record, so repeated polls can never
    flip between instruments. ``version`` increments on every change.
    """
    bot: NiftyBot = app.state.bot
    return await bot.get_symbol()


@app.post("/api/symbols/active")
async def set_active_symbol(request: SetActiveSymbolRequest):
    """Set the active trading symbol (persisted, versioned, audited)."""
    bot: NiftyBot = app.state.bot
    try:
        return await bot.set_symbol(
            request.symbol,
            request.symbol_name,
            changed_by=request.changed_by,
        )
    except SymbolConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except SymbolValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.get("/api/symbols/history")
async def get_symbol_history(limit: int = 20):
    """Audit trail of active-symbol changes (who/when/why)."""
    limit = max(1, min(limit, 200))
    return {"changes": await get_symbol_change_history(limit)}


@app.get("/api/watchlist")
async def get_watchlist():
    """Return all watchlist symbols with live quotes."""
    bot: NiftyBot = app.state.bot
    return {"quotes": bot.get_watchlist_quotes()}


# ── Bot Control ─────────────────────────────────────────────────

@app.post("/api/bot/start")
async def start_bot():
    bot: NiftyBot = app.state.bot
    if bot.is_running:
        return {"message": "Bot is already running", "uptime": bot.uptime}
    
    try:
        await bot.start()
    except EngineLockedError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    app.state.startup_error = None
    return {"message": "Bot started", "uptime": bot.uptime, "symbol": bot.symbol}


@app.post("/api/bot/stop")
async def stop_bot():
    bot: NiftyBot = app.state.bot
    if not bot.is_running:
        return {"message": "Bot is not running"}
    
    await bot.stop()
    return {"message": "Bot stopped", "today_pnl": round(bot.today_pnl, 2)}


@app.post("/api/bot/restart")
async def restart_bot():
    bot: NiftyBot = app.state.bot
    if bot.is_running:
        await bot.stop()
    try:
        await bot.start()
    except EngineLockedError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    app.state.startup_error = None
    return {"message": "Bot restarted", "uptime": bot.uptime, "symbol": bot.symbol}


# ── Risk Manager ────────────────────────────────────────────────

@app.get("/api/risk/status")
async def get_risk_status():
    rm = app.state.bot.risk_manager
    can_trade, reason = await rm.can_trade()
    return {
        "can_trade": can_trade,
        "reason": reason,
        "daily_pnl": round(rm.daily_pnl, 2),
        "weekly_pnl": round(rm.weekly_pnl, 2),
        "trades_today": rm.trades_today,
        "consecutive_losses": rm.consecutive_losses,
        "cooldown_until": rm.cooldown_until.isoformat() if rm.cooldown_until else None,
    }


# ── WebSocket Live Feed ─────────────────────────────────────────

@app.websocket("/ws/live")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    bot: NiftyBot = app.state.bot
    bot.add_websocket_client(websocket)
    try:
        # Tell the client which instrument it is looking at right away, so it
        # never has to guess (and can detect later `symbol_changed` events).
        await websocket.send_json({
            "type": "hello",
            "server_time": datetime.now().isoformat(),
            "active_symbol": await bot.get_symbol(),
        })
        while True:
            # Keep connection alive, bot pushes data automatically
            # Can also handle client commands here
            data = await websocket.receive_text()
            # Echo back or handle commands
            await websocket.send_json({"type": "ack", "received": data})
    except WebSocketDisconnect:
        bot.remove_websocket_client(websocket)
    except Exception:
        bot.remove_websocket_client(websocket)


# ── MCP Server Mount ────────────────────────────────────────────

app.mount("/mcp", mcp_app)


# ── Flutter Web Build (optional) ────────────────────────────────

# Only mount if build directory exists
import os
flutter_build_path = "flutter_client/build/web"
if os.path.exists(flutter_build_path):
    app.mount("/", StaticFiles(directory=flutter_build_path, html=True), name="static")
