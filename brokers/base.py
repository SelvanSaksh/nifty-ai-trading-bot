from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional, Dict, Any
from models.trade import Trade
from models.candle import Candle

class BaseBroker(ABC):
    @abstractmethod
    async def connect(self) -> bool:
        """Establish connection to broker."""
        pass
    
    @abstractmethod
    async def disconnect(self):
        """Close connection."""
        pass
    
    @abstractmethod
    async def subscribe_ticks(self, symbols: list[str]):
        """Subscribe to live tick data via WebSocket."""
        pass
    
    @abstractmethod
    async def get_historical_candles(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        end_time: Optional[datetime] = None,
    ) -> list[Candle]:
        """
        Fetch historical OHLCV data.

        ``end_time`` (optional) bounds the window from above, so clients can
        page backwards in history.  When omitted the window ends "now".
        """
        pass
    
    @abstractmethod
    async def place_order(self, trade: Trade) -> Dict[str, Any]:
        """Place a market/limit order."""
        pass
    
    @abstractmethod
    async def modify_order(self, order_id: str, **kwargs) -> bool:
        """Modify stop-loss or target."""
        pass
    
    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        """Cancel pending order."""
        pass
    
    @abstractmethod
    async def get_positions(self) -> list[Dict[str, Any]]:
        """Get current open positions."""
        pass
    
    @abstractmethod
    async def get_funds(self) -> Dict[str, float]:
        """Get available funds."""
        pass