"""
Mock Fyers broker for testing without real API.
Generates simulated market data for backend development.
"""
import asyncio
import random
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Callable

from brokers.base import BaseBroker
from models.candle import Candle
from models.trade import Trade
from config import settings


class FyersBroker(BaseBroker):
    """Mock Fyers broker - simulates market data for testing."""
    
    def __init__(self):
        self.is_connected = False
        self.tick_callback: Optional[Callable] = None
        self._price = 24500.0
        self._running = False
        self._symbols: List[str] = []
        self._session: Optional[asyncio.Task] = None
    
    async def connect(self) -> bool:
        self.is_connected = True
        print("[MOCK] Fyers connected (simulated)")
        return True
    
    async def disconnect(self):
        self.is_connected = False
        self._running = False
        if self._session:
            self._session.cancel()
        print("[MOCK] Fyers disconnected")
    
    async def subscribe_ticks(self, symbols: list[str]):
        self._symbols = symbols
        self._running = True
        self._session = asyncio.create_task(self._generate_mock_ticks())
        print(f"[MOCK] Subscribed to: {symbols}")
    
    async def _generate_mock_ticks(self):
        """Generate simulated ticks every second."""
        while self._running and self.is_connected:
            # Random walk price
            self._price += random.uniform(-5, 5)
            tick = {
                "ltp": round(self._price, 2),
                "v": random.randint(100, 1000),
                "symbol": self._symbols[0] if self._symbols else "NSE:NIFTY50-INDEX"
            }
            
            if self.tick_callback:
                await self.tick_callback(tick)
            
            await asyncio.sleep(1)
    
    async def get_historical_candles(
        self, 
        symbol: str = "NSE:NIFTY50-INDEX", 
        timeframe: str = "15", 
        limit: int = 100
    ) -> list[Candle]:
        """Generate mock historical candles."""
        candles = []
        base_price = 24500.0
        
        for i in range(limit):
            ts = datetime.now() - timedelta(minutes=int(timeframe) * (limit - i))
            open_p = base_price + random.uniform(-50, 50)
            close = open_p + random.uniform(-30, 30)
            high = max(open_p, close) + random.uniform(0, 20)
            low = min(open_p, close) - random.uniform(0, 20)
            
            candles.append(Candle(
                timestamp=ts,
                open=round(open_p, 2),
                high=round(high, 2),
                low=round(low, 2),
                close=round(close, 2),
                volume=random.randint(10000, 100000),
                timeframe=timeframe
            ))
            base_price = close
        
        return candles
    
    async def place_order(self, trade: Trade) -> Dict[str, Any]:
        """Mock order placement."""
        return {
            "s": "ok",
            "id": f"PAPER_{trade.id}",
            "message": "Paper trade executed",
            "order_id": f"MOCK_{datetime.now().strftime('%H%M%S')}"
        }
    
    async def modify_order(self, order_id: str, **kwargs) -> bool:
        print(f"[MOCK] Modified order: {order_id}")
        return True
    
    async def cancel_order(self, order_id: str) -> bool:
        print(f"[MOCK] Cancelled order: {order_id}")
        return True
    
    async def get_positions(self) -> list[Dict[str, Any]]:
        return []
    
    async def get_funds(self) -> Dict[str, float]:
        return {"available": settings.CAPITAL, "used": 0.0}