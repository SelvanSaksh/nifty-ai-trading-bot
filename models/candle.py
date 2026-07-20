from pydantic import BaseModel
from datetime import datetime
from typing import Optional

class Candle(BaseModel):
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    timeframe: str = "15m"
    
    ema_20: Optional[float] = None
    ema_50: Optional[float] = None
    ema_200: Optional[float] = None
    rsi: Optional[float] = None
    macd: Optional[float] = None
    macd_signal: Optional[float] = None
    macd_hist: Optional[float] = None
    atr: Optional[float] = None
    vwap: Optional[float] = None
    adx: Optional[float] = None
    bb_upper: Optional[float] = None
    bb_lower: Optional[float] = None