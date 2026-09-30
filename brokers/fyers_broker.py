
import asyncio
import os
import time
import aiohttp
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Callable, List

from brokers.base import BaseBroker
from models.candle import Candle
from models.trade import Trade
from config import settings


def normalize_tick(msg: Any) -> Optional[Dict[str, Any]]:
    """Map a Fyers data-socket payload onto the tick shape the engine consumes.

    The official socket also delivers control frames (auth ack, subscribe ack);
    those carry no price and are dropped here.
    """
    if not isinstance(msg, dict):
        return None
    symbol = msg.get("symbol")
    ltp = msg.get("ltp")
    if not symbol or not ltp:
        return None
    return {
        "symbol": symbol,
        "ltp": float(ltp),
        "v": int(msg.get("vol_traded_today") or 0),
        "open": float(msg.get("open_price") or 0),
        "high": float(msg.get("high_price") or 0),
        "low": float(msg.get("low_price") or 0),
        "prev_close": float(msg.get("prev_close_price") or 0),
        "ch": msg.get("ch"),
        "chp": msg.get("chp"),
        "type": msg.get("type"),
    }


class FyersBroker(BaseBroker):
    """Fyers broker via HTTP REST API."""

    BASE_URL = settings.FYERS_API_URL
    DATA_URL = settings.FYERS_API_URL

    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.tick_callback: Optional[Callable] = None
        self.is_connected = False
        self._headers = {}
        self._data_socket = None
        self._subscribed_symbols: set = set()
        self._pending_symbols: List[str] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None
    
    def _get_auth_header(self) -> str:
        token = settings.FYERS_ACCESS_TOKEN
        if not token:
            # The OAuth callback persists the full token locally. Prefer an
            # explicitly configured environment value, then recover that token
            # for normal local restarts without falling back to fake prices.
            from fyers_auth import load_token
            token, _ = load_token()
        if not token:
            return ""
        
        # If token doesn't start with app_id, prepend it
        if not settings.FYERS_APP_ID or token.startswith(settings.FYERS_APP_ID):
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
        socket, self._data_socket = self._data_socket, None
        self._subscribed_symbols.clear()
        self._pending_symbols = []
        if socket is not None:
            try:
                await asyncio.get_running_loop().run_in_executor(None, socket.close_connection)
            except Exception as e:
                print(f"[FYERS] Data socket close failed: {e}")
        if self.session:
            await self.session.close()
            self.session = None

    def _build_data_socket(self, data_ws, token: str):
        """Official Fyers data socket wired into the engine's tick callback."""
        # The SDK decodes the JWT itself, so the "APP-200:" prefix must be stripped.
        jwt = token.split(":", 1)[1] if ":" in token else token
        loop = self._loop

        def _on_connect():
            try:
                # connect() sleeps 2s before this fires; wait for the handshake so
                # the subscribe frames are not wiped by the socket's startup queue.
                for _ in range(50):
                    if self._data_socket is None or self._data_socket.is_connected():
                        break
                    time.sleep(0.1)
                pending = list(self._pending_symbols)
                if pending and self._data_socket is not None:
                    self._data_socket.subscribe(symbols=pending, data_type="SymbolUpdate")
                    self._subscribed_symbols.update(pending)
                    self._pending_symbols = []
                    print(f"[FYERS] Live data socket subscribed to {len(pending)} symbols")
            except Exception as e:
                print(f"[FYERS] Data socket subscribe failed: {e}")

        def _on_message(msg):
            tick = normalize_tick(msg)
            if tick is None or self.tick_callback is None:
                return
            # Runs on the SDK's reader thread — hand the coroutine to the loop.
            asyncio.run_coroutine_threadsafe(self.tick_callback(tick), loop)

        def _on_error(msg):
            print(f"[FYERS WS] {msg}")

        def _on_close(msg):
            print(f"[FYERS WS] closed: {msg}")

        os.makedirs("logs", exist_ok=True)
        return data_ws.FyersDataSocket(
            access_token=jwt,
            log_path="logs",
            litemode=False,
            write_to_file=False,
            reconnect=True,
            on_connect=_on_connect,
            on_message=_on_message,
            on_error=_on_error,
            on_close=_on_close,
        )

    async def subscribe_ticks(self, symbols: list[str]):
        """Live ticks via the official Fyers data socket (HSM feed)."""
        loop = asyncio.get_running_loop()
        self._loop = loop

        token = self._get_auth_header()
        if not token:
            print("[FYERS] No access token — run `python fyers_auth.py` or GET /api/auth/login")
            return

        try:
            from fyers_apiv3.FyersWebsocket import data_ws
        except ImportError as e:
            print(f"[FYERS] fyers-apiv3 unavailable ({e}); live ticks are disabled")
            return

        if self._data_socket is None:
            self._pending_symbols = list(symbols)
            self._data_socket = self._build_data_socket(data_ws, token)
            try:
                await loop.run_in_executor(None, self._data_socket.connect)
            except Exception as e:
                print(f"[FYERS] Data socket connect failed: {e}")
                self._data_socket = None
            return

        # Socket already running: only add the symbols added since last time.
        missing = [s for s in symbols if s not in self._subscribed_symbols]
        if not missing:
            return
        socket = self._data_socket
        try:
            await loop.run_in_executor(
                None, lambda: socket.subscribe(symbols=missing, data_type="SymbolUpdate")
            )
        except Exception as e:
            print(f"[FYERS] Data socket subscribe failed: {e}")
        else:
            self._subscribed_symbols.update(missing)
    
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
        # The REST endpoint uses epoch UTC timestamps. Keep every internal
        # candle timestamp in naïve UTC because that is the API's documented
        # JSON contract to the web client.
        end = end_time or datetime.utcnow()
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
                        timestamp=datetime.utcfromtimestamp(c[0]),
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
