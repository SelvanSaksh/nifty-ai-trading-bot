from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from models.enums import Direction

class Signal(BaseModel):
    timestamp: datetime           # naive UTC
    direction: Direction
    score: int                    # 0-100
    confidence: float             # 0.0-1.0
    reasoning: str                # Human-readable rationale
    
    trend_score: int = 0
    vwap_score: int = 0
    rsi_score: int = 0
    macd_score: int = 0
    volume_score: int = 0
    pattern_score: int = 0
    advanced_score: int = 0
    
    stop_loss: float
    target_1: float
    target_2: float
    target_3: float
    risk_reward: float
    # Close of the candle the signal was evaluated on (None: older records).
    price: Optional[float] = None
    
    ht_filter_passed: bool = True

    # Instrument the signal was generated for (history must stay unambiguous)
    symbol: str = ""
    symbol_name: str = ""