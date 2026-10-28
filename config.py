"""
Centralized configuration for Nifty 50 AI Trading Bot.
Loads from environment variables with sensible defaults.
"""
from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    # ── Fyers API Credentials ───────────────────────────────────
    FYERS_APP_ID: str = ""
    FYERS_SECRET: str = ""
    FYERS_REDIRECT_URI: str = "https://api.trading.quantumvora.com/api/auth/callback"
    FYERS_ACCESS_TOKEN: str = ""
    FYERS_REFRESH_TOKEN: str = ""
    # Web terminal origin. Used as the post-login landing page when the OAuth
    # callback cannot recover where the browser came from.
    FRONTEND_URL: str = ""
    # FYERS API v3 production host (FYERS has no sandbox; this is the live
    # endpoint used by the official SDK for trading and market data).
    FYERS_API_URL: str = "https://api-t1.fyers.in/api/v3"
    # Market-data host. Candles live here, NOT on the trading host — the SDK
    # declares `DATA_API = "https://api-t1.fyers.in/data"` separately, and
    # `api/v3/history` answers a bare 404 "page not found".
    FYERS_DATA_URL: str = "https://api-t1.fyers.in/data"
    # Real market data is the production default. The mock broker is only for
    # local demos and tests; it generates random candles and must never power a
    # chart intended to match an exchange chart.
    BROKER_MODE: str = "fyers"
    
    # ── Trading Configuration ───────────────────────────────────
    TRADING_SYMBOL: str = "NSE:NIFTY50-INDEX"
    # Market structure is computed from every candle since this date (IST),
    # never from a fixed number of recent candles: a fixed start keeps every
    # confirmed swing, BOS and CHoCH stable for good.
    STRUCTURE_START: str = "2026-01-01"
    TRADING_SYMBOL_NAME: str = "Nifty 50"
    PAPER_TRADING: bool = True
    CAPITAL: float = 100_000.0
    RISK_PER_TRADE: float = 0.01
    MAX_DAILY_LOSS: float = 0.025
    MAX_WEEKLY_LOSS: float = 0.05
    MAX_TRADES_PER_DAY: int = 5
    COOLDOWN_MINUTES: int = 30
    NEWS_BLACKOUT_MINUTES: int = 15
    
    # ── Signal Thresholds ───────────────────────────────────────
    MIN_SCORE_THRESHOLD: int = 55
    MIN_RISK_REWARD: float = 1.5
    
    # ── Timeframes ──────────────────────────────────────────────
    PRIMARY_TIMEFRAME: str = "15"
    HIGHER_TIMEFRAME: str = "60"
    
    # ── Telegram Notifications ──────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""
    
    # ── Database ────────────────────────────────────────────────
    DB_PATH: str = "data/trades.db"
    
    # ── Trading Session (IST) ───────────────────────────────────
    TRADING_START_TIME: str = "09:15"
    TRADING_END_TIME: str = "15:30"
    # An open intraday trade is closed at this time, and no new trade opens
    # from then on. Fyers squares INTRADAY positions off itself around 15:20
    # and charges for it.
    SQUARE_OFF_TIME: str = "15:15"
    # Quantity step for instruments without their own lot: index paper
    # trades simulate futures in lots of this size. Set it to the current
    # exchange lot. Equities (-EQ) always trade in single shares.
    LOT_SIZE: int = 50
    
    # ── Server ──────────────────────────────────────────────────
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    DEBUG: bool = False
    
    # ── Admin ───────────────────────────────────────────────────
    ADMIN_SECRET_KEY: str = "change-me-in-production"
    
    # ── Logging ─────────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


# Singleton instance
settings = Settings()


def public_config() -> dict:
    """The rules the engine trades by, safe to show anyone: no credentials,
    tokens or keys. Served by GET /api/config and the MCP config tool."""
    return {
        "paper_trading": settings.PAPER_TRADING,
        "broker_mode": settings.BROKER_MODE,
        "trading_symbol": settings.TRADING_SYMBOL,
        "capital": settings.CAPITAL,
        "risk_per_trade": settings.RISK_PER_TRADE,
        "max_daily_loss": settings.MAX_DAILY_LOSS,
        "max_weekly_loss": settings.MAX_WEEKLY_LOSS,
        "max_trades_per_day": settings.MAX_TRADES_PER_DAY,
        "cooldown_minutes": settings.COOLDOWN_MINUTES,
        "news_blackout_minutes": settings.NEWS_BLACKOUT_MINUTES,
        "min_score_threshold": settings.MIN_SCORE_THRESHOLD,
        "min_risk_reward": settings.MIN_RISK_REWARD,
        "primary_timeframe": settings.PRIMARY_TIMEFRAME,
        "higher_timeframe": settings.HIGHER_TIMEFRAME,
        "trading_start_time": settings.TRADING_START_TIME,
        "trading_end_time": settings.TRADING_END_TIME,
        "square_off_time": settings.SQUARE_OFF_TIME,
        "lot_size": settings.LOT_SIZE,
    }


def ensure_data_dir():
    """Ensure data directory exists."""
    Path("data").mkdir(exist_ok=True)
