"""The live tick socket must survive reconnects, re-logins and restarts."""
import asyncio
from datetime import datetime

import pytest

from brokers import fyers_broker
from brokers.fyers_broker import FyersBroker, nse_session_open
from config import settings

SYMBOLS = ["NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX", "NSE:RELIANCE-EQ"]
TICK = {"symbol": "NSE:NIFTY50-INDEX", "ltp": 25000.5}


class FakeSocket:
    """Mimics the parts of fyers_apiv3's FyersDataSocket the broker relies on,
    including its singleton constructor and its reconnect behaviour."""

    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.restart_flag = kwargs.get("reconnect", True)
        self.connected = False
        self.subscriptions = []
        self.closed = 0

    # SDK API
    def connect(self):
        self.connected = True
        self.kwargs["on_connect"]()

    def is_connected(self):
        return self.connected

    def subscribe(self, symbols, data_type="SymbolUpdate"):
        self.subscriptions.append(list(symbols))

    def close_connection(self):
        # The real SDK does nothing unless the socket is currently connected.
        if self.connected:
            self.restart_flag = False
            self.connected = False
            self.closed += 1

    # What the SDK does after a dropped connection: forget every
    # subscription, reconnect, and call on_connect again.
    def simulate_reconnect(self):
        self.connected = True
        self.kwargs["on_connect"]()

    def deliver(self, message):
        self.kwargs["on_message"](message)


class FakeDataWs:
    FyersDataSocket = FakeSocket


@pytest.fixture
def broker(monkeypatch, tmp_path):
    FakeSocket._instance = None
    monkeypatch.setenv("FYERS_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "FYERS_ACCESS_TOKEN", "APP-200:header.payload.sig")
    monkeypatch.setattr(settings, "FYERS_APP_ID", "APP-200")
    monkeypatch.setattr("fyers_auth.load_token", lambda: ("", None))
    # The broker imports the SDK lazily; hand it the fake one.
    import sys
    import types

    package = types.ModuleType("fyers_apiv3.FyersWebsocket")
    package.data_ws = FakeDataWs
    monkeypatch.setitem(sys.modules, "fyers_apiv3.FyersWebsocket", package)
    monkeypatch.setattr(fyers_broker.time, "sleep", lambda _: None)
    b = FyersBroker()
    yield b
    FakeSocket._instance = None


class TestReconnect:
    async def test_every_reconnect_resubscribes_all_symbols(self, broker):
        await broker.subscribe_ticks(SYMBOLS)
        socket = broker._data_socket
        assert socket.subscriptions == [SYMBOLS]

        # Regression: subscriptions used to be sent only once ("pending"
        # symbols), so a reconnected socket received no ticks at all.
        socket.simulate_reconnect()
        socket.simulate_reconnect()
        assert socket.subscriptions == [SYMBOLS, SYMBOLS, SYMBOLS]

    async def test_symbols_added_later_survive_a_reconnect(self, broker):
        await broker.subscribe_ticks(SYMBOLS[:1])
        await broker.subscribe_ticks(SYMBOLS)
        socket = broker._data_socket
        assert socket.subscriptions == [SYMBOLS[:1], SYMBOLS[1:]]

        socket.simulate_reconnect()
        assert socket.subscriptions[-1] == SYMBOLS


class TestRelogin:
    async def test_relogin_builds_a_new_socket_with_every_symbol(self, broker):
        await broker.subscribe_ticks(SYMBOLS)
        old = broker._data_socket

        await broker.reauthenticate()
        await broker.subscribe_ticks(SYMBOLS)
        new = broker._data_socket

        # Regression: the SDK class is a singleton, so "building a new socket"
        # used to re-initialise the old object under its running threads.
        assert new is not old
        assert old.restart_flag is False, "the old socket must stop reconnecting"
        assert new.subscriptions == [SYMBOLS]

    async def test_a_socket_mid_reconnect_is_stopped_too(self, broker):
        await broker.subscribe_ticks(SYMBOLS)
        old = broker._data_socket
        old.connected = False  # dropped; the SDK is retrying in the background

        await broker.reauthenticate()

        # close_connection() is a no-op here, so the flag must be cleared directly.
        assert old.restart_flag is False

    async def test_an_old_socket_neither_delivers_ticks_nor_lingers(self, broker):
        received = []

        async def on_tick(tick):
            received.append(tick)

        broker.tick_callback = on_tick
        await broker.subscribe_ticks(SYMBOLS)
        old = broker._data_socket
        await broker.reauthenticate()
        await broker.subscribe_ticks(SYMBOLS)
        new = broker._data_socket

        old.deliver(TICK)
        new.deliver(TICK)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(received) == 1, "ticks from the replaced socket must be dropped"

        # The orphan coming back from its own reconnect closes itself.
        old.simulate_reconnect()
        assert old.connected is False
        assert old.subscriptions == [SYMBOLS]

    async def test_disconnect_forgets_the_symbols(self, broker):
        await broker.subscribe_ticks(SYMBOLS)
        await broker.disconnect()
        assert broker._data_socket is None
        assert broker._wanted_symbols == []


# Monday 13 Oct 2026 - 11:00 IST is 05:30 UTC.
SESSION = datetime(2026, 10, 13, 5, 30)


class TestStallWatchdog:
    def test_session_hours(self):
        assert nse_session_open(SESSION)
        assert not nse_session_open(datetime(2026, 10, 13, 3, 30))  # 09:00 IST
        assert not nse_session_open(datetime(2026, 10, 13, 10, 0))  # 15:30 IST
        assert not nse_session_open(datetime(2026, 10, 11, 5, 30))  # Sunday

    async def test_silence_in_market_hours_is_a_stall(self, broker):
        await broker.subscribe_ticks(SYMBOLS)
        start = broker._socket_started_at
        assert not broker.feed_stalled(now=start + 60, now_utc=SESSION)
        assert broker.feed_stalled(now=start + 121, now_utc=SESSION)
        # Outside the session no ticks are expected.
        assert not broker.feed_stalled(now=start + 10_000, now_utc=datetime(2026, 10, 13, 12, 0))

    async def test_ticks_keep_the_feed_healthy(self, broker):
        await broker.subscribe_ticks(SYMBOLS)
        socket = broker._data_socket
        socket.deliver(TICK)
        last = broker._last_tick_at
        assert not broker.feed_stalled(now=last + 100, now_utc=SESSION)
        assert broker.feed_stalled(now=last + 130, now_utc=SESSION)

    async def test_a_stalled_feed_is_rebuilt_and_resubscribed(self, broker, monkeypatch):
        await broker.subscribe_ticks(SYMBOLS)
        old = broker._data_socket
        monkeypatch.setattr(broker, "feed_stalled", lambda: True)

        assert await broker.ensure_live_feed() is True
        new = broker._data_socket
        assert new is not old
        assert old.restart_flag is False
        assert new.subscriptions == [SYMBOLS]
        # Backs off while the silence continues (e.g. a market holiday).
        assert broker._feed_retry_seconds == 240

    async def test_a_healthy_feed_is_left_alone(self, broker, monkeypatch):
        await broker.subscribe_ticks(SYMBOLS)
        old = broker._data_socket
        monkeypatch.setattr(broker, "feed_stalled", lambda: False)
        assert await broker.ensure_live_feed() is False
        assert broker._data_socket is old
