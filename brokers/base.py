from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional, Dict, Any
from models.trade import Trade
from models.candle import Candle

#: Normalized order states every broker reports from ``get_order``.
ORDER_PENDING = "PENDING"
ORDER_FILLED = "FILLED"
ORDER_CANCELLED = "CANCELLED"
ORDER_REJECTED = "REJECTED"
#: States an order never leaves.
ORDER_FINAL = (ORDER_FILLED, ORDER_CANCELLED, ORDER_REJECTED)


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
    async def place_market_order(self, symbol: str, side: int, qty: int) -> Dict[str, Any]:
        """Intraday market order; ``side`` is 1 to buy, -1 to sell.

        Returns the broker reply: ``{"s": "ok", "id": ...}`` on acceptance.
        Acceptance is not a fill - ``get_order`` tells whether it filled.
        """
        pass

    @abstractmethod
    async def place_stop_order(self, symbol: str, side: int, qty: int, stop_price: float) -> Dict[str, Any]:
        """Intraday stop-loss market order (SL-M) triggered at ``stop_price``."""
        pass

    @abstractmethod
    async def get_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        """``{"id", "status", "filled_qty", "price", "message"}`` (status is
        one of the ORDER_* states; price is the average fill price or None),
        or None when the order cannot be read."""
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
        """Current positions: ``{"symbol", "net_qty", "product"}`` each.
        Raises when they cannot be read (never answers "flat" by mistake)."""
        pass
    
    @abstractmethod
    async def get_funds(self) -> Dict[str, float]:
        """Get available funds."""
        pass