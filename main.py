from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from contextlib import asynccontextmanager

from bot import NiftyBot
from database import init_db
from trading_mcp.server import mcp_app, set_bot_instance
from config import settings, ensure_data_dir
from fyers_auth import exchange_code_for_token, save_token


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage bot lifecycle: start on startup, stop on shutdown."""
    # Startup
    ensure_data_dir()
    await init_db()
    
    app.state.bot = NiftyBot()
    set_bot_instance(app.state.bot)  # Link to MCP server
    await app.state.bot.start()
    
    yield
    
    # Shutdown
    await app.state.bot.stop()


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
    return {
        "status": "healthy",
        "bot_running": bot.is_running,
        "uptime": bot.uptime,
        "websocket_clients": len(bot.ws_clients)
    }


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

        return HTMLResponse(
            f"<h2>Auth Successful</h2>"
            f"<p>Token saved. You can close this tab.</p>"
            f"<p>Fyers ID: {params.get('state', 'N/A')}</p>",
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
    return {
        "running": bot.is_running,
        "uptime": bot.uptime,
        "active_trade": bot.active_trade.model_dump() if bot.active_trade else None,
        "paper_mode": settings.PAPER_TRADING,
        "today_pnl": round(bot.today_pnl, 2),
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

@app.get("/api/candles")
async def get_candles(timeframe: str = "15m", limit: int = 100):
    return await app.state.bot.get_candles(timeframe, limit)


# ── Market Context ──────────────────────────────────────────────

@app.get("/api/market-context")
async def get_market_context():
    return await app.state.bot.get_market_context()


# ── Bot Control ─────────────────────────────────────────────────

@app.post("/api/bot/start")
async def start_bot():
    bot: NiftyBot = app.state.bot
    if bot.is_running:
        return {"message": "Bot is already running", "uptime": bot.uptime}
    
    await bot.start()
    return {"message": "Bot started", "uptime": bot.uptime}


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
    await bot.start()
    return {"message": "Bot restarted", "uptime": bot.uptime}


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
    app.state.bot.add_websocket_client(websocket)
    try:
        while True:
            # Keep connection alive, bot pushes data automatically
            # Can also handle client commands here
            data = await websocket.receive_text()
            # Echo back or handle commands
            await websocket.send_json({"type": "ack", "received": data})
    except WebSocketDisconnect:
        app.state.bot.remove_websocket_client(websocket)


# ── MCP Server Mount ────────────────────────────────────────────

app.mount("/mcp", mcp_app)


# ── Flutter Web Build (optional) ────────────────────────────────

# Only mount if build directory exists
import os
flutter_build_path = "flutter_client/build/web"
if os.path.exists(flutter_build_path):
    app.mount("/", StaticFiles(directory=flutter_build_path, html=True), name="static")