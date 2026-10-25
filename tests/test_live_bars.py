"""Forming candles of non-engine symbols must follow their live ticks."""
from datetime import datetime, timedelta

import pytest

from models.candle import Candle
from utils.live_bars import LiveBarTracker, in_session

SYMBOL = "NSE:INFY-EQ"
FIVE = timedelta(minutes=5)
# Wed 2026-10-14 10:00 IST = 04:30 UTC (in session).
BAR = datetime(2026, 10, 14, 4, 30)


def bar(start, o=100.0, h=101.0, l=99.0, c=100.5, v=1000, tf="5m"):
    return Candle(
        timestamp=start, open=o, high=h, low=l, close=c, volume=v,
        timeframe=tf, symbol=SYMBOL, symbol_name="Infosys",
    )


def history():
    return [bar(BAR - FIVE, 99.0, 100.0, 98.5, 99.5, 800), bar(BAR)]


class TestSession:
    def test_session_bounds(self):
        assert in_session(datetime(2026, 10, 14, 3, 45))
        assert in_session(datetime(2026, 10, 14, 9, 59))
        assert not in_session(datetime(2026, 10, 14, 3, 44))
        assert not in_session(datetime(2026, 10, 14, 10, 0))
        assert not in_session(datetime(2026, 10, 17, 5, 0))  # Saturday


class TestExtendLastBar:
    def test_ticks_after_fetch_move_high_low_close_and_volume(self):
        t = LiveBarTracker()
        fetched = BAR + timedelta(minutes=1)
        t.record(SYMBOL, 100.4, 50_000, fetched - timedelta(seconds=1))
        mark = t.volume_mark(SYMBOL)
        t.record(SYMBOL, 101.8, 50_300, fetched + timedelta(seconds=20))
        t.record(SYMBOL, 98.7, 50_450, fetched + timedelta(seconds=50))
        t.record(SYMBOL, 99.9, 50_600, fetched + timedelta(seconds=70))

        out = t.apply(SYMBOL, history(), FIVE, fetched, mark, fetched + timedelta(seconds=75))

        last = out[-1]
        assert (last.timestamp, last.open) == (BAR, 100.0)  # history owns the open
        assert last.high == 101.8
        assert last.low == 98.7
        assert last.close == 99.9
        assert last.volume == 1000 + (50_600 - 50_000)
        assert len(out) == 2
        assert out[0] == history()[0]

    def test_ticks_older_than_the_fetch_never_override_close_or_volume(self):
        t = LiveBarTracker()
        fetched = BAR + timedelta(minutes=2)
        t.record(SYMBOL, 100.2, 40_000, BAR + timedelta(seconds=30))
        out = t.apply(SYMBOL, history(), FIVE, fetched, 40_000, fetched)
        assert out[-1] == history()[-1]

    def test_input_is_not_mutated(self):
        t = LiveBarTracker()
        candles = history()
        t.record(SYMBOL, 150.0, 1, BAR + timedelta(minutes=2))
        out = t.apply(SYMBOL, candles, FIVE, BAR, None, BAR + timedelta(minutes=2))
        assert candles[-1].high == 101.0
        assert out[-1].high == 150.0
        assert out[-1].volume == 1000  # no volume mark: volume left alone

    def test_ticks_of_other_symbols_or_bars_are_ignored(self):
        t = LiveBarTracker()
        t.record("NSE:TCS-EQ", 500.0, 1, BAR + timedelta(minutes=1))
        t.record(SYMBOL, 120.0, 1, BAR - timedelta(minutes=1))  # previous bar
        out = t.apply(SYMBOL, history(), FIVE, BAR, None, BAR + timedelta(minutes=2))
        assert out[-1] == history()[-1]

    def test_out_of_session_ticks_are_ignored(self):
        t = LiveBarTracker()
        night = datetime(2026, 10, 14, 12, 0)
        t.record(SYMBOL, 150.0, 1, night)
        assert t.apply(SYMBOL, history(), FIVE, BAR, None, night) == history()

    def test_exchange_time_places_the_tick(self):
        t = LiveBarTracker()
        # Received just after the bar closed, traded inside it.
        t.record(SYMBOL, 103.0, 1, BAR + FIVE + timedelta(milliseconds=200),
                 exchange_at=BAR + FIVE - timedelta(seconds=1))
        out = t.apply(SYMBOL, history(), FIVE, BAR, None, BAR + FIVE)
        assert out[-1].high == 103.0
        assert out[-1].close == 103.0


class TestFormingBar:
    def _streaming(self, t, until):
        """Ticks every 10s from before the last bar up to ``until``."""
        at, vol, price = BAR - timedelta(seconds=30), 10_000, 100.0
        while at <= until:
            t.record(SYMBOL, price, vol, at)
            at += timedelta(seconds=10)
            vol += 10
            price += 0.05
        return vol - 10

    def test_next_bar_is_formed_from_ticks_until_history_has_it(self):
        t = LiveBarTracker()
        now = BAR + FIVE + timedelta(seconds=45)
        self._streaming(t, BAR + FIVE - timedelta(seconds=1))
        t.record(SYMBOL, 104.0, 20_000, BAR + FIVE + timedelta(seconds=3))
        t.record(SYMBOL, 105.5, 20_200, BAR + FIVE + timedelta(seconds=20))
        t.record(SYMBOL, 103.5, 20_350, BAR + FIVE + timedelta(seconds=40))

        out = t.apply(SYMBOL, history(), FIVE, BAR, None, now)

        assert len(out) == 3
        forming = out[-1]
        assert forming.timestamp == BAR + FIVE
        assert forming.open == 104.0
        assert forming.high == 105.5
        assert forming.low == 103.5
        assert forming.close == 103.5
        # Day volume before the bar's first tick -> latest day volume.
        last_before = max(m.day_volume for k, m in t._minutes[SYMBOL].items() if k < BAR + FIVE)
        assert forming.volume == 20_350 - last_before
        assert forming.symbol == SYMBOL and forming.timeframe == "5m"
        assert forming.ema_20 is None and forming.rsi is None

    def test_no_bar_without_seeing_its_open(self):
        t = LiveBarTracker()
        # First tick ever arrives mid-bar: its price is not the bar's open.
        t.record(SYMBOL, 104.0, 1, BAR + FIVE + timedelta(seconds=30))
        out = t.apply(SYMBOL, history(), FIVE, BAR, None, BAR + FIVE + timedelta(seconds=40))
        assert len(out) == 2

    def test_no_bar_after_a_feed_gap(self):
        t = LiveBarTracker()
        self._streaming(t, BAR + timedelta(minutes=2))
        t.reset_continuity()
        t.record(SYMBOL, 104.0, 1, BAR + FIVE + timedelta(seconds=30))
        out = t.apply(SYMBOL, history(), FIVE, BAR, None, BAR + FIVE + timedelta(seconds=40))
        assert len(out) == 2

    def test_never_bridges_more_than_one_missing_bar(self):
        t = LiveBarTracker()
        self._streaming(t, BAR + 2 * FIVE + timedelta(seconds=30))
        out = t.apply(SYMBOL, history(), FIVE, BAR, None, BAR + 2 * FIVE + timedelta(seconds=30))
        assert len(out) == 2

    def test_no_bar_past_the_session_close(self):
        t = LiveBarTracker()
        last = datetime(2026, 10, 14, 9, 55)  # 15:25 IST, the session's last 5m bar
        candles = [bar(last)]
        at = last - timedelta(seconds=30)
        while at < last + FIVE + timedelta(seconds=30):
            t.record(SYMBOL, 100.0, 1, at)
            at += timedelta(seconds=10)
        out = t.apply(SYMBOL, candles, FIVE, last, None, last + FIVE + timedelta(seconds=30))
        assert len(out) == 1


class TestEngineCandles:
    """get_candles serves non-engine symbols with their live forming bar."""

    @pytest.fixture()
    async def engine(self, db_path, monkeypatch):
        import bot as bot_module
        from database import init_db

        await init_db()

        now = {"t": BAR + timedelta(minutes=1)}

        class Clock(datetime):
            @classmethod
            def utcnow(cls):
                return now["t"]

        monkeypatch.setattr(bot_module, "datetime", Clock)
        engine = bot_module.NiftyBot()
        calls = []

        async def history_stub(symbol, timeframe, limit, end_time=None):
            calls.append(now["t"])
            # A full 200-bar window, as the broker serves it.
            older = [bar(BAR - FIVE * i, 99.0, 100.0, 98.5, 99.5, 800) for i in range(199, 1, -1)]
            bars = older + history()
            if end_time is not None:
                bars = [c for c in bars if c.timestamp <= end_time]
            return bars[-limit:]

        monkeypatch.setattr(engine.broker, "get_historical_candles", history_stub)
        return engine, now, calls

    async def _tick(self, engine, price, volume, at, now):
        now["t"] = at
        await engine._on_tick({"symbol": SYMBOL, "ltp": price, "v": volume})

    @pytest.mark.asyncio
    async def test_forming_bar_follows_ticks_between_cache_refreshes(self, engine):
        engine, now, calls = engine
        await self._tick(engine, 100.4, 50_000, BAR + timedelta(seconds=50), now)
        now["t"] = BAR + timedelta(minutes=1)
        first = await engine.get_candles("5m", 3, symbol=SYMBOL)
        assert first["candles"][-1]["close"] == 100.5

        await self._tick(engine, 102.25, 50_400, BAR + timedelta(minutes=1, seconds=10), now)
        polled = await engine.get_candles("5m", 3, symbol=SYMBOL)

        assert len(calls) == 1, "served from the cache, not a new broker call"
        last = polled["candles"][-1]
        assert last["timestamp"] == BAR
        assert last["open"] == 100.0
        assert last["high"] == 102.25
        assert last["close"] == 102.25
        assert last["volume"] == 1000 + 400
        assert polled["symbol"] == SYMBOL

    @pytest.mark.asyncio
    async def test_paging_with_end_time_is_untouched(self, engine):
        engine, now, _ = engine
        await self._tick(engine, 150.0, 1, BAR + timedelta(minutes=1, seconds=10), now)
        paged = await engine.get_candles(
            "5m", 2, symbol=SYMBOL, end_time=(BAR + FIVE).isoformat()
        )
        assert [c["close"] for c in paged["candles"]] == [99.5, 100.5]

    @pytest.mark.asyncio
    async def test_new_bar_triggers_one_refetch_then_forms_from_ticks(self, engine):
        engine, now, calls = engine
        at = BAR - timedelta(seconds=30)
        while at < BAR + FIVE:
            await self._tick(engine, 100.0, 1, at, now)
            at += timedelta(seconds=15)
        await engine.get_candles("5m", 3, symbol=SYMBOL)
        await self._tick(engine, 101.0, 2, BAR + FIVE + timedelta(seconds=5), now)

        now["t"] = BAR + FIVE + timedelta(seconds=8)
        rolled = await engine.get_candles("5m", 3, symbol=SYMBOL)
        now["t"] = BAR + FIVE + timedelta(seconds=12)
        again = await engine.get_candles("5m", 3, symbol=SYMBOL)

        assert len(calls) == 2, "one refetch for the new bar, then the cache"
        assert rolled["candles"][-1]["timestamp"] == BAR + FIVE
        assert rolled["candles"][-1]["open"] == 101.0
        assert again["candles"] == rolled["candles"]

    def test_exchange_time_is_read_from_the_tick(self):
        from bot import NiftyBot

        received = datetime(2026, 10, 14, 4, 35, 0, 200000)
        epoch = int((received - timedelta(seconds=1) - datetime(1970, 1, 1)).total_seconds())
        assert NiftyBot._exchange_time({"exch_time": epoch}, received) == datetime(2026, 10, 14, 4, 34, 59)
        assert NiftyBot._exchange_time({}, received) is None
        assert NiftyBot._exchange_time({"exch_time": "junk"}, received) is None
        future = epoch + 3600
        assert NiftyBot._exchange_time({"exch_time": future}, received) is None


class TestEngineTickVolume:
    """The feed reports cumulative day volume; candles need per-tick volume."""

    @pytest.fixture()
    async def engine(self, db_path):
        from bot import NiftyBot
        from database import init_db

        await init_db()
        engine = NiftyBot()
        engine.is_running = True
        engine.analyzer_symbol = engine.symbol
        return engine

    async def test_bar_volume_is_the_growth_of_day_volume(self, engine, monkeypatch):
        import bot as bot_module

        clock = {"t": BAR + timedelta(seconds=5)}

        class Clock(datetime):
            @classmethod
            def utcnow(cls):
                return clock["t"]

        monkeypatch.setattr(bot_module, "datetime", Clock)
        engine.analyzer.candles_5m = [bar(BAR, v=1000)]
        engine.analyzer.candles_15m = []
        engine.analyzer.candles_1h = []

        for i, day_volume in enumerate((50_000, 50_300, 50_300, 50_750)):
            clock["t"] = BAR + timedelta(seconds=5 + 10 * i)
            await engine._on_tick({"symbol": engine.symbol, "ltp": 100.0, "v": day_volume})

        # First tick only sets the baseline (history holds the volume so far).
        assert engine.analyzer.candles_5m[-1].volume == 1000 + 300 + 0 + 450

    def test_traded_since_last_tick(self, engine):
        day = datetime(2026, 10, 14, 4, 0)
        assert engine._traded_since_last_tick("X", 1_000, day) == 0
        assert engine._traded_since_last_tick("X", 1_250, day) == 250
        assert engine._traded_since_last_tick("X", 1_250, day) == 0
        assert engine._traded_since_last_tick("X", 1_100, day) == 0  # never negative
        assert engine._traded_since_last_tick("Y", 0, day) == 0
        # Next session: the counter restarts, all of it is new volume.
        assert engine._traded_since_last_tick("X", 400, day + timedelta(days=1)) == 400

    async def test_exchange_time_stamps_and_places_the_tick(self, engine, monkeypatch):
        import bot as bot_module

        # Traded 1s before the bar boundary, received just after it.
        traded = BAR + FIVE - timedelta(seconds=1)
        received = BAR + FIVE + timedelta(milliseconds=300)

        class Clock(datetime):
            @classmethod
            def utcnow(cls):
                return received

        monkeypatch.setattr(bot_module, "datetime", Clock)
        sent = []

        async def capture(data):
            sent.append(data)

        monkeypatch.setattr(engine, "_broadcast", capture)
        engine.analyzer.candles_5m = [bar(BAR)]
        engine.analyzer.candles_15m = []
        engine.analyzer.candles_1h = []
        epoch = int((traded - datetime(1970, 1, 1)).total_seconds())
        await engine._on_tick(
            {"symbol": engine.symbol, "ltp": 123.0, "v": 0, "exch_time": epoch}
        )

        assert [c.timestamp for c in engine.analyzer.candles_5m] == [BAR]
        assert engine.analyzer.candles_5m[-1].close == 123.0
        stamps = {m["type"]: m["timestamp"] for m in sent}
        assert stamps["watchlist_tick"] == traded.isoformat()
        assert stamps["tick"] == traded.isoformat()

    async def test_receive_time_is_the_fallback(self, engine, monkeypatch):
        import bot as bot_module

        received = BAR + timedelta(seconds=40)

        class Clock(datetime):
            @classmethod
            def utcnow(cls):
                return received

        monkeypatch.setattr(bot_module, "datetime", Clock)
        sent = []

        async def capture(data):
            sent.append(data)

        monkeypatch.setattr(engine, "_broadcast", capture)
        await engine._on_tick({"symbol": engine.symbol, "ltp": 123.0, "v": 0})

        assert sent[0]["timestamp"] == received.isoformat()


class TestEngineSeriesAfterOutage:
    """After a dead token or a stalled feed the engine's own series must not
    keep a hole (the chart showed no session at all after a re-login)."""

    @pytest.fixture()
    async def engine(self, db_path, monkeypatch):
        import bot as bot_module
        from database import init_db

        await init_db()
        engine = bot_module.NiftyBot()
        engine.is_running = True
        engine.analyzer_symbol = engine.symbol
        clock = {"t": BAR}

        class Clock(datetime):
            @classmethod
            def utcnow(cls):
                return clock["t"]

        monkeypatch.setattr(bot_module, "datetime", Clock)
        return engine, clock

    async def test_after_hours_ticks_form_no_bars(self, engine):
        engine, clock = engine
        engine.analyzer.candles_5m = [bar(BAR)]
        engine.analyzer.candles_15m = []
        engine.analyzer.candles_1h = []
        # 21:30 IST - Fyers still streams the index's last price.
        clock["t"] = datetime(2026, 10, 14, 16, 0, 5)
        await engine._on_tick({"symbol": engine.symbol, "ltp": 100.0, "v": 0})

        assert [c.timestamp for c in engine.analyzer.candles_5m] == [BAR]
        assert engine.analyzer.candles_15m == []

    async def test_relogin_backfills_the_missed_session(self, engine, monkeypatch):
        engine, _ = engine
        stale = [bar(BAR - FIVE * i) for i in range(5, 0, -1)]
        engine.analyzer.candles_5m = list(stale)
        engine.analyzer.candles_15m = [bar(BAR, tf="15m")]
        engine.analyzer.candles_1h = [bar(BAR, tf="1h")]
        full = stale + [bar(BAR + FIVE * i) for i in range(4)]

        async def history_stub(symbol, timeframe, limit, end_time=None):
            if timeframe == "60":
                raise ConnectionError("Fyers history request failed (HTTP 500)")
            return list(full)

        async def noop(*args, **kwargs):
            return None

        monkeypatch.setattr(engine.broker, "get_historical_candles", history_stub)
        monkeypatch.setattr(engine.broker, "reauthenticate", noop, raising=False)
        monkeypatch.setattr(engine.broker, "subscribe_ticks", noop)

        await engine.reauthenticate()

        assert [c.timestamp for c in engine.analyzer.candles_5m] == [c.timestamp for c in full]
        assert len(engine.analyzer.candles_15m) == len(full)
        # The 1h reload failed: its buffer is kept instead of wiped.
        assert [c.timeframe for c in engine.analyzer.candles_1h] == ["1h"]
