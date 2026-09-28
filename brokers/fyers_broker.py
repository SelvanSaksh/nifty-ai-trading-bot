
import asyncio
import aiohttp
import json
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Callable

from brokers.base import BaseBroker
from models.candle import Candle
from models.trade import Trade
from config import settings


class FyersBroker(BaseBroker):
    """Fyers broker via HTTP REST API."""
    
    BASE_URL = "https://api-t1.fyers.in/api/v3"  
    DATA_URL = "https://api-t1.fyers.in/api/v3"  
    WS_URL = "wss://socket.fyers.in/v3"
    
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self.ws_task: Optional[asyncio.Task] = None
        self.tick_callback: Optional[Callable] = None
        self.is_connected = False
        self._headers = {}
    
    def _get_auth_header(self) -> str:
        token = settings.FYERS_ACCESS_TOKEN
        
        # If token doesn't start with app_id, prepend it
        if token.startswith(settings.FYERS_APP_ID):
            return token
        
        return f"{settings.FYERS_APP_ID}:{token}"
    
    def _ensure_session(self) -> aiohttp.ClientSession:
        """Return a live HTTP session, creating one on demand (REST-only calls)."""
        if self.session is None or self.session.closed:
            self._headers = {
                "Authorization": self._get_auth_header(),
                "Content-Type": "application/json"
            }
            self.session = aiohttp.ClientSession(headers=self._headers)
        return self.session
    
    async def connect(self) -> bool:
        """Validate token by fetching profile."""
        auth_token = self._get_auth_header()
        
        self._headers = {
            "Authorization": auth_token,
            "Content-Type": "application/json"
        }
        
        self.session = aiohttp.ClientSession(headers=self._headers)
        
        try:
            # ✅ Correct endpoint: /profile
            async with self.session.get(f"{self.BASE_URL}/profile") as resp:
                data = await resp.json()
                print(f"[FYERS] Profile response: {data}")
                self.is_connected = data.get("s") == "ok"
                
                if not self.is_connected:
                    print(f"[FYERS] Connection failed: {data}")
                
                return self.is_connected
        except Exception as e:
            print(f"[FYERS] Connection error: {e}")
            return False
    
    async def disconnect(self):
        self.is_connected = False
        if self.ws_task and not self.ws_task.done():
            self.ws_task.cancel()
            try:
                await self.ws_task
            except asyncio.CancelledError:
                pass
        self.ws_task = None
        if self.ws:
            await self.ws.close()
            self.ws = None
        if self.session:
            await self.session.close()
            self.session = None
    
    async def subscribe_ticks(self, symbols: list[str]):
        """WebSocket tick subscription (replaces any previous subscription)."""
        if self.ws_task and not self.ws_task.done():
            # One subscription at a time — otherwise ticks arrive doubled after
            # a symbol switch.
            self.ws_task.cancel()
            try:
                await self.ws_task
            except asyncio.CancelledError:
                pass

        auth_token = self._get_auth_header()
        
        async def ws_listener():
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(
                    f"{self.WS_URL}?token={auth_token}"
                ) as ws:
                    self.ws = ws
                    
                    await ws.send_json({
                        "T": "SUBS",
                        "symbols": symbols
                    })
                    
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            if self.tick_callback:
                                await self.tick_callback(data)
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            break
        
        self.ws_task = asyncio.create_task(ws_listener())
    
    async def get_historical_candles(
        self, 
        symbol: str = "NSE:NIFTY50-INDEX", 
        timeframe: str = "15", 
        limit: int = 100,
        end_time: Optional[datetime] = None,
    ) -> list[Candle]:
        """Fetch historical candles via REST."""
        from utils.helpers import timeframe_label, TIMEFRAME_MINUTES

        session = self._ensure_session()
        end = end_time or datetime.now()
        minutes = int(timeframe) if str(timeframe).isdigit() else TIMEFRAME_MINUTES.get(timeframe, 15)
        label = timeframe_label(minutes)

        from_date = (end - timedelta(days=max(limit // 2, 1))).strftime("%Y-%m-%d")
        to_date = end.strftime("%Y-%m-%d")
        
        params = {
            "symbol": symbol,
            "resolution": timeframe,
            "date_format": "1",
            "range_from": from_date,
            "range_to": to_date,
            "cont_flag": "1"
        }
        
        async with session.get(f"{self.DATA_URL}/history", params=params) as resp:
            data = await resp.json()
            candles = []
            
            if data.get("s") == "ok":
                for c in data.get("candles", []):
                    candles.append(Candle(
                        timestamp=datetime.fromtimestamp(c[0]),
                        open=c[1], high=c[2], low=c[3],
                        close=c[4], volume=c[5],
                        timeframe=label,
                        symbol=symbol,
                    ))
            
            if end_time is not None:
                candles = [c for c in candles if c.timestamp <= end_time]

            candles.sort(key=lambda c: c.timestamp)
            if limit and len(candles) > limit:
                candles = candles[-limit:]

            return candles
    
    async def place_order(self, trade: Trade) -> Dict[str, Any]:
        """Place order via REST API."""
        if settings.PAPER_TRADING:
            return {"s": "ok", "id": f"PAPER_{trade.id}", "message": "Paper trade"}
        
        order_data = {
            "symbol": trade.symbol,
            "qty": trade.quantity,
            "type": 2,
            "side": 1 if trade.direction.value == "LONG" else -1,
            "productType": "INTRADAY",
            "limitPrice": 0,
            "stopPrice": 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
            "stopLoss": trade.stop_loss,
            "takeProfit": trade.target_1
        }
        
        async with self.session.post(f"{self.BASE_URL}/orders", json=order_data) as resp:
            return await resp.json()
    
    async def modify_order(self, order_id: str, **kwargs) -> bool:
        data = {"id": order_id, **kwargs}
        async with self.session.patch(f"{self.BASE_URL}/orders", json=data) as resp:
            result = await resp.json()
            return result.get("s") == "ok"
    
    async def cancel_order(self, order_id: str) -> bool:
        async with self.session.delete(f"{self.BASE_URL}/orders", params={"id": order_id}) as resp:
            result = await resp.json()
            return result.get("s") == "ok"
    
    async def get_positions(self) -> list[Dict[str, Any]]:
        async with self.session.get(f"{self.BASE_URL}/positions") as resp:
            data = await resp.json()
            return data.get("netPositions", [])
    
    async def get_funds(self) -> Dict[str, float]:
        async with self.session.get(f"{self.BASE_URL}/funds") as resp:
            data = await resp.json()
            funds = data.get("fund_limit", [])
            return {
                "available": next((f.get("equityAmount", 0) for f in funds if f.get("id") == 10), 0),
                "used": next((f.get("equityAmount", 0) for f in funds if f.get("id") == 11), 0)
            }