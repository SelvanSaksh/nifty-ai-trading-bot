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
    
    # Realistic starting prices for each symbol
    _DEFAULT_PRICES: Dict[str, float] = {
        "NSE:NIFTY50-INDEX": 24500.0,
        "NSE:NIFTYBANK-INDEX": 51000.0,
        "NSE:FINNIFTY-INDEX": 23000.0,
        "NSE:SENSEX-INDEX": 81000.0,
        "NSE:RELIANCE-EQ": 2950.0,
        "NSE:TCS-EQ": 3800.0,
        "NSE:INFY-EQ": 1650.0,
        "NSE:HDFCBANK-EQ": 1700.0,
        "NSE:ICICIBANK-EQ": 1250.0,
        "NSE:SBIN-EQ": 820.0,
        "NSE:BAJFINANCE-EQ": 7200.0,
        "NSE:KOTAKBANK-EQ": 1800.0,
        "NSE:ITC-EQ": 460.0,
        "NSE:HINDUNILVR-EQ": 2500.0,
        "NSE:LT-EQ": 3400.0,
    }
    
    def __init__(self):
        self.is_connected = False
        self.tick_callback: Optional[Callable] = None
        self._price = 24500.0
        self._running = False
        self._symbols: List[str] = []
        self._session: Optional[asyncio.Task] = None
        self._symbol_prices: Dict[str, float] = {}
    
    async def connect(self) -> bool:
        self.is_connected = True
        print("[MOCK] Fyers connected (simulated)")
        return True
    
    async def disconnect(self):
        self.is_connected = False
        self._running = False
        if self._session and not self._session.done():
            self._session.cancel()
            try:
                await self._session
            except asyncio.CancelledError:
                pass
        self._session = None
        print("[MOCK] Fyers disconnected")
    
    async def subscribe_ticks(self, symbols: list[str]):
        # Cancel any previous tick loop — resubscribing must never stack
        # generators on top of each other (that doubles every tick).
        if self._session and not self._session.done():
            self._session.cancel()
            try:
                await self._session
            except asyncio.CancelledError:
                pass

        self._symbols = list(symbols)
        self._running = True
        # Initialize per-symbol starting prices with realistic values
        for sym in symbols:
            if sym not in self._symbol_prices:
                self._symbol_prices[sym] = self._DEFAULT_PRICES.get(sym, 24500.0)
        self._session = asyncio.create_task(self._generate_mock_ticks())
        print(f"[MOCK] Subscribed to: {symbols}")
    
    async def _generate_mock_ticks(self):
        """Generate simulated ticks every second."""
        while self._running and self.is_connected:
            for sym in self._symbols:
                price = self._symbol_prices.get(sym, 24500.0)
                # Volatility proportional to price level (~0.02% per tick)
                volatility = max(price * 0.0002, 0.1)
                price += random.uniform(-volatility, volatility)
                self._symbol_prices[sym] = round(price, 2)
                tick = {
                    "ltp": round(price, 2),
                    "v": random.randint(100, 5000),
                    "symbol": sym
                }
                
                if self.tick_callback:
                    await self.tick_callback(tick)
            
            await asyncio.sleep(1)
    
    async def get_historical_candles(
        self, 
        symbol: str = "NSE:NIFTY50-INDEX", 
        timeframe: str = "15", 
        limit: int = 100,
        end_time: Optional[datetime] = None,
    ) -> list[Candle]:
        """Generate mock historical candles ending at ``end_time`` (default: now)."""
        from utils.helpers import timeframe_label

        minutes = int(timeframe) if str(timeframe).isdigit() else 15
        label = timeframe_label(minutes)
        end = end_time or datetime.now()

        candles = []
        # Fall back to the instrument's realistic base price, never a generic one.
        base_price = self._symbol_prices.get(symbol) or self._DEFAULT_PRICES.get(symbol, 24500.0)

        for i in range(limit):
            # Bars are aligned to their timeframe boundary, like real data.
            ts = end - timedelta(minutes=minutes * (limit - 1 - i))
            ts = ts.replace(second=0, microsecond=0)
            ts = ts - timedelta(minutes=ts.minute % minutes)

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
                timeframe=label,
                symbol=symbol,
            ))
            base_price = close

        candles.sort(key=lambda c: c.timestamp)
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