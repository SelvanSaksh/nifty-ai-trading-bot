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
    FYERS_REDIRECT_URI: str = "http://127.0.0.1:8000/api/auth/callback"
    FYERS_ACCESS_TOKEN: str = ""
    FYERS_REFRESH_TOKEN: str = ""
    
    # ── Trading Configuration ───────────────────────────────────
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


def ensure_data_dir():
    """Ensure data directory exists."""
    Path("data").mkdir(exist_ok=True)