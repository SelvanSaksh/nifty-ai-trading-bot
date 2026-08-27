from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional
from models.enums import Direction, TradeStatus, SignalResult

class Trade(BaseModel):
    id: str = Field(default_factory=lambda: f"TRD_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    entry_time: datetime
    direction: Direction
    entry_price: float
    quantity: int
    stop_loss: float
    target_1: float
    target_2: float
    target_3: float
    
    # Position tracking
    initial_qty: int
    remaining_qty: int
    status: TradeStatus = TradeStatus.OPEN
    
    # Exits
    exit_t1_price: Optional[float] = None
    exit_t1_qty: Optional[int] = None
    exit_t1_time: Optional[datetime] = None
    
    exit_t2_price: Optional[float] = None
    exit_t2_qty: Optional[int] = None
    exit_t2_time: Optional[datetime] = None
    
    exit_t3_price: Optional[float] = None
    exit_t3_qty: Optional[int] = None
    exit_t3_time: Optional[datetime] = None
    
    exit_sl_price: Optional[float] = None
    exit_sl_time: Optional[datetime] = None
    
    # P&L
    realized_pnl: float = 0.0
    result: Optional[SignalResult] = None
    
    # Metadata
    symbol: str = "NSE:NIFTY50-INDEX"
    symbol_name: str = "Nifty 50"
    signal_score: int
    confidence: float
    paper_trade: bool = True