
import asyncio
import json
import os
import tempfile
import time
import aiohttp
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Callable, List

from brokers.base import BaseBroker
from models.candle import Candle
from models.trade import Trade
from config import settings


#: Fyers refused the credentials — the client must send the user to /api/auth/login.
TOKEN_EXPIRED_MESSAGE = (
    "Fyers access token expired — reconnect Fyers to reload market data"
)


class FyersAuthError(ConnectionError):
    """Raised when Fyers rejects the access token used for a request."""


#: File the Fyers SDK opens inside ``log_path`` as soon as the data socket is built.
DATA_SOCKET_LOG_FILE = "fyersDataSocket.log"


def writable_log_dir(preferred: Optional[str] = None) -> str:
    """Directory the Fyers data socket can write its log file to.

    The SDK opens ``<log_path>/fyersDataSocket.log`` while the socket is being
    constructed (even with ``write_to_file=False``). In Docker, ``/app/logs``
    used to be a root-owned bind mount that the non-root ``botuser`` cannot
    write, so building the socket raised ``PermissionError`` and the whole
    engine fell back to API-only mode: no live ticks, no live candles, no
    quotes. A log file must never stop trading, so an unwritable directory
    falls back to a private temp directory instead.
    """
    candidates = [
        preferred or os.environ.get("FYERS_LOG_DIR") or "logs",
        os.path.join(tempfile.gettempdir(), "fyers-logs"),
    ]
    for path in candidates:
        try:
            os.makedirs(path, exist_ok=True)
            # Open the real log file (not just the directory): a root-owned
            # file left by an earlier run is just as fatal as a root-owned dir.
            with open(os.path.join(path, DATA_SOCKET_LOG_FILE), "a"):
                pass
            if path != candidates[0]:
                print(f"[FYERS] Data socket logs go to {path!r} instead")
            return path
        except OSError as e:
            print(f"[FYERS] Log directory {path!r} is not writable ({e})")
    return tempfile.gettempdir()


#: Silence on the live feed (during market hours) after which the data socket
#: is treated as dead and rebuilt. Indices tick several times a second.
TICK_STALL_SECONDS = 120
#: Longest wait between rebuild attempts while the feed stays silent (e.g. a
#: market holiday, when no ticks are expected at all).
MAX_FEED_RETRY_SECONDS = 900
#: ``close_connection`` joins the SDK's threads without a timeout.
SOCKET_CLOSE_TIMEOUT = 15
#: Reconnect attempts the SDK makes on its own before giving up (its default
#: of 5 is used up by one short network outage, after which it never
#: reconnects again).
SOCKET_RECONNECT_RETRY = 50


def nse_session_open(now_utc: Optional[datetime] = None) -> bool:
    """True during the NSE cash session (Mon-Fri 09:15-15:30 IST)."""
    now = now_utc or datetime.utcnow()
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    # 09:15 IST = 03:45 UTC, 15:30 IST = 10:00 UTC.
    return 3 * 60 + 45 <= minutes < 10 * 60


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
    DATA_URL = settings.FYERS_DATA_URL

    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.tick_callback: Optional[Callable] = None
        self.is_connected = False
        self._headers = {}
        self._data_socket = None
        # Every symbol the engine wants ticks for. The SDK forgets its
        # subscriptions whenever it reconnects, so this list - not "what is
        # still pending" - is what each (re)connect subscribes.
        self._wanted_symbols: List[str] = []
        self._subscribed_symbols: set = set()
        # Bumped for every socket built or closed; callbacks of an older
        # socket see a different number and stay silent instead of doubling ticks.
        self._socket_generation = 0
        self._last_tick_at: Optional[float] = None
        self._socket_started_at: Optional[float] = None
        self._feed_retry_seconds = TICK_STALL_SECONDS
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Authorization value the current session was built with, so a re-login
        # can be noticed and the session rebuilt.
        self._auth_token: Optional[str] = None

    def _get_auth_header(self) -> str:
        # One source of truth for "which token is current", so the credentials
        # on the wire always match what /api/auth/status reports.
        from fyers_auth import active_token

        token, _, _ = active_token()
        if not token:
            return ""
        
        # If token doesn't start with app_id, prepend it
        if not settings.FYERS_APP_ID or token.startswith(settings.FYERS_APP_ID):
            return token
        
        return f"{settings.FYERS_APP_ID}:{token}"
    
    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Return a live HTTP session carrying the token that is active *now*.

        A re-login swaps the credentials while the process keeps running, so the
        session is rebuilt as soon as it no longer matches the current token —
        otherwise every request would keep shipping the expired one.
        """
        token = self._get_auth_header()
        if self.session is not None and not self.session.closed and token != self._auth_token:
            stale, self.session = self.session, None
            await stale.close()
        if self.session is None or self.session.closed:
            self._auth_token = token
            self._headers = {
                "Authorization": token,
                "Content-Type": "application/json"
            }
            self.session = aiohttp.ClientSession(headers=self._headers)
        return self.session

    async def reauthenticate(self) -> None:
        """Forget cached credentials so the next call uses the new token."""
        if self.session is not None and not self.session.closed:
            await self.session.close()
        self.session = None
        self._auth_token = None
        # The socket authenticated with the old token and must be replaced,
        # but the symbols it served are still wanted.
        await self._close_data_socket()
    
    async def connect(self) -> bool:
        """Validate token by fetching profile."""
        auth_token = self._get_auth_header()

        if not auth_token:
            self.is_connected = False
            print("[FYERS] No access token — GET /api/auth/login to authenticate")
            return False

        self._auth_token = auth_token
        self._headers = {
            "Authorization": auth_token,
            "Content-Type": "application/json"
        }
        
        self.session = aiohttp.ClientSession(headers=self._headers)
        
        try:
            # ✅ Correct endpoint: /profile
            async with self.session.get(f"{self.BASE_URL}/profile") as resp:
                status = resp.status
                raw = await resp.text()
            if status in (401, 403):
                # The credentials are the problem, not the network — publish it
                # so /api/auth/status sends clients to the login flow at once.
                from fyers_auth import mark_token_rejected
                mark_token_rejected()
                print("[FYERS] Access token rejected — GET /api/auth/login")
            # Fyers answers a dead token with 401 and sometimes no body at all,
            # so never assume the reply is JSON.
            try:
                data = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                data = None
            if not isinstance(data, dict):
                self.is_connected = False
                print(f"[FYERS] Profile unavailable (HTTP {status}) — access token expired?")
                return False

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
        self._wanted_symbols = []
        await self._close_data_socket()
        if self.session:
            await self.session.close()
            self.session = None

    async def _close_data_socket(self) -> None:
        """Stop the current data socket for good (no SDK auto-reconnect)."""
        socket, self._data_socket = self._data_socket, None
        self._socket_generation += 1  # silence callbacks still in flight
        self._subscribed_symbols.clear()
        self._socket_started_at = None
        if socket is None:
            return
        # close_connection() only acts while the socket is *connected*: a
        # socket inside its own reconnect loop kept reconnecting next to the
        # new one (two feeds, one of them on the old token).
        try:
            socket.restart_flag = False
        except Exception:
            pass
        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, socket.close_connection), SOCKET_CLOSE_TIMEOUT
            )
        except asyncio.TimeoutError:
            print("[FYERS] Data socket did not close in time; abandoning it")
        except Exception as e:
            print(f"[FYERS] Data socket close failed: {e}")

    def _build_data_socket(self, data_ws, token: str):
        """Official Fyers data socket wired into the engine's tick callback."""
        # The SDK decodes the JWT itself, so the "APP-200:" prefix must be stripped.
        jwt = token.split(":", 1)[1] if ":" in token else token
        loop = self._loop
        self._socket_generation += 1
        generation = self._socket_generation
        holder: Dict[str, Any] = {}

        def _current() -> bool:
            return generation == self._socket_generation

        def _on_connect():
            # Fires on the first connect AND after every SDK auto-reconnect.
            # The SDK drops all its subscriptions when it reconnects, so the
            # full wanted list is subscribed every time. (Subscribing only the
            # symbols still "pending" left every reconnected socket - after a
            # re-login, restart or network blip - with nothing subscribed.)
            socket = holder.get("socket")
            try:
                if socket is None:
                    return
                if not _current():
                    # An orphan from before a re-login/restart came back up.
                    socket.restart_flag = False
                    socket.close_connection()
                    return
                # connect() sleeps 2s before this fires; wait for the handshake so
                # the subscribe frames are not wiped by the socket's startup queue.
                for _ in range(50):
                    if socket.is_connected():
                        break
                    time.sleep(0.1)
                symbols = list(self._wanted_symbols)
                if symbols:
                    socket.subscribe(symbols=symbols, data_type="SymbolUpdate")
                    self._subscribed_symbols = set(symbols)
                    print(f"[FYERS] Live data socket subscribed to {len(symbols)} symbols")
            except Exception as e:
                print(f"[FYERS] Data socket subscribe failed: {e}")

        def _on_message(msg):
            if not _current():
                return
            tick = normalize_tick(msg)
            if tick is None:
                return
            self._last_tick_at = time.monotonic()
            self._feed_retry_seconds = TICK_STALL_SECONDS
            if self.tick_callback is None or loop is None:
                return
            # Runs on the SDK's reader thread — hand the coroutine to the loop.
            asyncio.run_coroutine_threadsafe(self.tick_callback(tick), loop)

        def _on_error(msg):
            print(f"[FYERS WS] {msg}")

        def _on_close(msg):
            print(f"[FYERS WS] closed: {msg}")

        # FyersDataSocket is a singleton: constructing it again re-ran __init__
        # on the *same* object while the old socket's threads were still using
        # it (resetting their reconnect flag and token underneath them). Every
        # socket built here must be a new object.
        socket_class = data_ws.FyersDataSocket
        if hasattr(socket_class, "_instance"):
            socket_class._instance = None

        socket = socket_class(
            access_token=jwt,
            log_path=writable_log_dir(),
            litemode=False,
            write_to_file=False,
            reconnect=True,
            reconnect_retry=SOCKET_RECONNECT_RETRY,
            on_connect=_on_connect,
            on_message=_on_message,
            on_error=_on_error,
            on_close=_on_close,
        )
        holder["socket"] = socket
        return socket

    async def subscribe_ticks(self, symbols: list[str]):
        """Live ticks via the official Fyers data socket (HSM feed)."""
        loop = asyncio.get_running_loop()
        self._loop = loop
        for symbol in symbols:
            if symbol not in self._wanted_symbols:
                self._wanted_symbols.append(symbol)

        if self._data_socket is None:
            await self._open_data_socket()
            return

        # Socket already running: only add the symbols added since last time.
        missing = [s for s in self._wanted_symbols if s not in self._subscribed_symbols]
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

    async def _open_data_socket(self) -> bool:
        """Build and connect a data socket for every wanted symbol."""
        loop = asyncio.get_running_loop()
        self._loop = loop
        self._last_tick_at = None
        # Starts the stall clock even when the build fails, so the watchdog retries.
        self._socket_started_at = time.monotonic()

        token = self._get_auth_header()
        if not token:
            print("[FYERS] No access token — run `python fyers_auth.py` or GET /api/auth/login")
            return False

        try:
            from fyers_apiv3.FyersWebsocket import data_ws
        except ImportError as e:
            print(f"[FYERS] fyers-apiv3 unavailable ({e}); live ticks are disabled")
            return False

        try:
            # Building the socket can fail too (it opens its log file);
            # that must cost live ticks, not the whole engine.
            self._data_socket = self._build_data_socket(data_ws, token)
            await loop.run_in_executor(None, self._data_socket.connect)
        except Exception as e:
            print(f"[FYERS] Data socket unavailable, live ticks disabled: {e}")
            self._data_socket = None
            return False
        return True

    def feed_stalled(self, now: Optional[float] = None, now_utc: Optional[datetime] = None) -> bool:
        """True when the market is open but no tick arrived for too long.

        Catches every way the feed dies without an error reaching the engine:
        the SDK giving up after its reconnect attempts, a socket that came back
        without subscriptions, or Fyers dropping the connection server side.
        """
        if not self._wanted_symbols or not nse_session_open(now_utc):
            return False
        now = time.monotonic() if now is None else now
        last = self._last_tick_at or self._socket_started_at
        return last is None or now - last >= self._feed_retry_seconds

    async def ensure_live_feed(self) -> bool:
        """Rebuild the data socket when the feed has stalled. True if rebuilt."""
        if not self.feed_stalled():
            return False
        silent = self._feed_retry_seconds
        print(f"[FYERS] No live ticks for {silent}s during market hours — rebuilding data socket")
        await self._close_data_socket()
        # Back off while it stays silent (holiday, Fyers outage); a tick resets it.
        self._feed_retry_seconds = min(silent * 2, MAX_FEED_RETRY_SECONDS)
        await self._open_data_socket()
        return True
    
    async def _rejects_current_token(self, status: int) -> bool:
        """True when Fyers said these credentials are unusable.

        Fyers is inconsistent about *how* it reports a dead token: ``/profile``
        answers 401 JSON while some endpoints answer a bare non-JSON body.
        Anything ambiguous is therefore decided by asking ``/profile`` directly.
        """
        from fyers_auth import active_token, mark_token_rejected, verify_token

        token, _, _ = active_token()
        if not token:
            return True
        if status in (401, 403):
            mark_token_rejected()
            return True
        return await verify_token(token, force=True) is False

    async def get_historical_candles(
        self, 
        symbol: str = "NSE:NIFTY50-INDEX", 
        timeframe: str = "15", 
        limit: int = 100,
        end_time: Optional[datetime] = None,
    ) -> list[Candle]:
        """Fetch historical candles via REST."""
        from utils.helpers import timeframe_label, TIMEFRAME_MINUTES

        session = await self._ensure_session()
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

        candles = await self._history(session, params, symbol, label)

        if end_time is not None:
            candles = [c for c in candles if c.timestamp <= end_time]

        candles.sort(key=lambda c: c.timestamp)
        if limit and len(candles) > limit:
            candles = candles[-limit:]

        return candles

    #: Fyers serves at most 100 days of intraday candles per request.
    HISTORY_CHUNK_DAYS = 90

    async def get_history_range(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> list[Candle]:
        """Every candle from ``start`` to ``end`` (naive UTC), oldest first.

        Uses epoch bounds (``date_format=0``) so the range is exact, split
        into chunks the history endpoint accepts.
        """
        from utils.helpers import timeframe_label, TIMEFRAME_MINUTES

        session = await self._ensure_session()
        minutes = int(timeframe) if str(timeframe).isdigit() else TIMEFRAME_MINUTES.get(timeframe, 15)
        label = timeframe_label(minutes)
        epoch = datetime(1970, 1, 1)
        by_time: Dict[datetime, Candle] = {}
        chunk_start = start
        while chunk_start <= end:
            chunk_end = min(end, chunk_start + timedelta(days=self.HISTORY_CHUNK_DAYS))
            params = {
                "symbol": symbol,
                "resolution": timeframe,
                "date_format": "0",
                "range_from": str(int((chunk_start - epoch).total_seconds())),
                "range_to": str(int((chunk_end - epoch).total_seconds())),
                "cont_flag": "1",
            }
            for candle in await self._history(session, params, symbol, label):
                if start <= candle.timestamp <= end:
                    by_time[candle.timestamp] = candle
            chunk_start = chunk_end + timedelta(seconds=1)
        return [by_time[t] for t in sorted(by_time)]

    async def _history(self, session, params: Dict[str, Any], symbol: str, label: str) -> list[Candle]:
        """One /history request, parsed (auth failures raise FyersAuthError)."""
        async with session.get(f"{self.DATA_URL}/history", params=params) as resp:
            status = resp.status
            # Never `resp.json()` here: a rejected token makes Fyers answer
            # `text/plain`, and aiohttp's ContentTypeError then swallowed the
            # real reason and left clients with an empty chart.
            raw = await resp.text()

        try:
            data = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            data = None

        if not isinstance(data, dict):
            if await self._rejects_current_token(status):
                raise FyersAuthError(TOKEN_EXPIRED_MESSAGE)
            raise ConnectionError(f"Fyers history request failed (HTTP {status})")

        candles = []
        if data.get("s") in ("ok", "no_data"):
            # "no_data" is a legitimate empty result (e.g. a freshly-listed
            # instrument), not an error. Fyers can echo back the same bar
            # twice (seen with cont_flag=1 over wider ranges) - dedupe by
            # epoch so a repeated bar never renders as a doubled-up candle.
            seen_epochs = set()
            for c in data.get("candles", []):
                epoch = c[0]
                if epoch in seen_epochs:
                    continue
                seen_epochs.add(epoch)
                candles.append(Candle(
                    timestamp=datetime.utcfromtimestamp(epoch),
                    open=c[1], high=c[2], low=c[3],
                    close=c[4], volume=c[5],
                    timeframe=label,
                    symbol=symbol,
                ))
        elif await self._rejects_current_token(status):
            raise FyersAuthError(TOKEN_EXPIRED_MESSAGE)
        else:
            # `s != ok` carries the real reason (e.g. code -16 "Could not
            # authenticate the user"). Returning [] instead would leave the
            # chart blank with no message to show.
            raise ConnectionError(
                f"Fyers history request failed: {data.get('message') or data.get('code')}"
            )
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
        
        session = await self._ensure_session()
        async with session.post(f"{self.BASE_URL}/orders", json=order_data) as resp:
            return await resp.json()
    
    async def modify_order(self, order_id: str, **kwargs) -> bool:
        data = {"id": order_id, **kwargs}
        session = await self._ensure_session()
        async with session.patch(f"{self.BASE_URL}/orders", json=data) as resp:
            result = await resp.json()
            return result.get("s") == "ok"
    
    async def cancel_order(self, order_id: str) -> bool:
        session = await self._ensure_session()
        async with session.delete(f"{self.BASE_URL}/orders", params={"id": order_id}) as resp:
            result = await resp.json()
            return result.get("s") == "ok"
    
    async def get_positions(self) -> list[Dict[str, Any]]:
        session = await self._ensure_session()
        async with session.get(f"{self.BASE_URL}/positions") as resp:
            data = await resp.json()
            return data.get("netPositions", [])
    
    async def get_funds(self) -> Dict[str, float]:
        session = await self._ensure_session()
        async with session.get(f"{self.BASE_URL}/funds") as resp:
            data = await resp.json()
            funds = data.get("fund_limit", [])
            return {
                "available": next((f.get("equityAmount", 0) for f in funds if f.get("id") == 10), 0),
                "used": next((f.get("equityAmount", 0) for f in funds if f.get("id") == 11), 0)
            }
