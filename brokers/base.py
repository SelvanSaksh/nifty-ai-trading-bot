from abc import ABC, abstractmethod
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
    async def get_historical_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        """Fetch historical OHLCV data."""
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