"""The watchlist scanner: the live engine's verdict on every symbol's newest
closed 15m candle, ranked, scanned once per closed bar."""
from datetime import datetime, timedelta

from backtest.data import synthetic
from backtest.replay import OfflineAnalyzer, minute_ticks
from decision_engine import DecisionEngine
from models.candle import Candle
import asyncio

import scanner as scanner_module
from scanner import MANUAL_COOLDOWN, RETRY_AFTER, Scanner, scan_key
from utils.live_bars import in_session

#: Scanner.run as written (the conftest swaps it for an idle loop).
_REAL_RUN = Scanner.run

SYMBOLS = [
    ("NSE:RELIANCE-EQ", "Reliance", "EQUITY"),
    ("NSE:SBIN-EQ", "SBI", "EQUITY"),
    ("NSE:NIFTYBANK-INDEX", "Bank Nifty", "INDEX"),
]


def plain(c: Candle) -> Candle:
    """A candle as broker history serves it: prices only, no indicators."""
    return Candle(timestamp=c.timestamp, open=c.open, high=c.high, low=c.low,
                  close=c.close, volume=c.volume, timeframe=c.timeframe)


async def test_the_scanner_scores_a_symbol_as_the_live_engine_does():
    # Live: 1m history replayed tick by tick through the engine's analyzer.
    analyzer = OfflineAnalyzer(max_bars=400)
    engine = DecisionEngine()
    engine.min_threshold = 40  # so the comparison covers real signals too
    compared, signals = 0, 0
    for minute in synthetic(days=14):
        if not in_session(minute.timestamp):
            continue
        for tick in minute_ticks(minute):
            closed = analyzer.add_tick(tick.price, 0, tick.time)
            if closed is None or len(analyzer.candles_15m) < 220:
                continue
            live = await analyzer.analyze(closed)
            want = engine.evaluate(live)

            # Scanner: the same candles as plain history, forming bars included.
            history = {
                "15m": [plain(c) for c in analyzer.candles_15m],
                "1h": [plain(c) for c in analyzer.candles_1h],
            }

            async def fetch(_symbol, tf, history=history):
                return history[tf]

            scanner = Scanner(fetch, lambda: SYMBOLS[:1], engine, pace_seconds=0)
            got = await scanner.scan_symbol("NSE:RELIANCE-EQ", "Reliance", "EQUITY", tick.time)

            assert got["error"] is None
            assert got["candle_time"] == closed.timestamp.isoformat()
            assert (got["long_score"], got["short_score"]) == (
                engine._score_long(live), engine._score_short(live),
            )
            assert (got["signal"], got["score"], got["reason"]) == (
                want.direction.value, want.score, want.reasoning,
            )
            compared += 1
            signals += want.direction.value != "WAIT"
    assert compared >= 50 and signals >= 1, (compared, signals)


def bars(close: float, count: int = 230, step: float = 1.0, start=datetime(2026, 10, 20, 3, 45)):
    """``count`` 15m bars trending by ``step`` a bar to ``close`` (down if
    negative), zig-zagging 4 points so RSI stays out of overbought / oversold."""
    out = []
    for i in range(count):
        c = close - step * (count - 1 - i) + (0 if (count - 1 - i) % 2 == 0 else 4)
        out.append(Candle(timestamp=start + timedelta(minutes=15 * i), open=c - step / 2,
                          high=c + 1, low=c - 1, close=c, volume=0, timeframe="15m"))
    return out


def fake_history(by_symbol):
    async def fetch(symbol, tf):
        value = by_symbol[symbol]
        if isinstance(value, Exception):
            raise value
        return value if tf == "15m" else []
    return fetch


NOW = datetime(2026, 10, 30, 10, 0)  # after every bar above


async def test_a_scan_ranks_signals_first_then_scores_and_errors_last():
    engine = DecisionEngine()
    # Reliance scores LONG 40 / SHORT 18 and Bank Nifty LONG 0 / SHORT 29:
    # at 30, only Reliance is a signal.
    engine.min_threshold = 30
    scanner = Scanner(
        fake_history({
            "NSE:RELIANCE-EQ": bars(1400, step=0.5),        # steady uptrend
            "NSE:SBIN-EQ": ConnectionError("history unavailable"),
            "NSE:NIFTYBANK-INDEX": bars(51000, step=-2.0),  # downtrend
        }),
        lambda: SYMBOLS, engine, pace_seconds=0,
    )
    assert await scanner.scan(NOW) is True
    snap = scanner.snapshot()
    assert snap["scanned_at"] == NOW.isoformat() and snap["scanning"] is False
    assert (snap["timeframe"], snap["threshold"]) == ("15m", 30)
    ranked = [(r["symbol"], r.get("signal")) for r in snap["results"]]
    assert ranked == [
        ("NSE:RELIANCE-EQ", "LONG"), ("NSE:NIFTYBANK-INDEX", "WAIT"), ("NSE:SBIN-EQ", None),
    ]
    assert snap["results"][-1]["error"] == "history unavailable"
    first = snap["results"][0]
    assert (first["long_score"], first["short_score"], first["score"]) == (40, 18, 40)
    assert first["price"] == 1400.0 and first["error"] is None
    assert set(first) >= {"long_score", "short_score", "score", "reason",
                          "candle_time", "higher_timeframe_trend", "name", "instrument_type"}


async def test_a_symbol_without_closed_candles_is_reported_not_skipped():
    scanner = Scanner(fake_history({"NSE:RELIANCE-EQ": []}), lambda: SYMBOLS[:1],
                      DecisionEngine(), pace_seconds=0)
    await scanner.scan(NOW)
    assert scanner.results == [{
        "symbol": "NSE:RELIANCE-EQ", "name": "Reliance", "instrument_type": "EQUITY",
        "error": "No closed 15m candles",
    }]


async def test_the_forming_bar_is_never_scored():
    history = bars(1400, step=0.5)
    forming_start = history[-1].timestamp
    scanner = Scanner(fake_history({"NSE:RELIANCE-EQ": history}), lambda: SYMBOLS[:1],
                      DecisionEngine(), pace_seconds=0)
    # Ten minutes into the newest bar: the bar before it is the one scored.
    await scanner.scan(forming_start + timedelta(minutes=10))
    assert scanner.results[0]["candle_time"] == (forming_start - timedelta(minutes=15)).isoformat()


def test_one_scan_per_closed_bar_during_the_session_and_one_per_day_outside():
    bar = datetime(2026, 11, 2, 4, 30)  # Monday 10:00 IST
    # The 09:45 bar closes at 10:00; it is asked for 30 s later.
    assert scan_key(bar + timedelta(seconds=10)) == bar - timedelta(minutes=15)
    assert scan_key(bar + timedelta(seconds=30)) == bar
    assert scan_key(bar + timedelta(minutes=14, seconds=29)) == bar
    after_close = datetime(2026, 11, 2, 10, 0, 30)  # 15:30:30 IST
    evening = datetime(2026, 11, 2, 16, 0)
    assert scan_key(after_close) == scan_key(evening) == ("closed", bar.date())
    assert scan_key(datetime(2026, 11, 7, 6, 0)) == ("closed", datetime(2026, 11, 7).date())


async def test_scan_if_due_skips_a_bar_already_scanned():
    calls = []

    async def fetch(symbol, tf):
        calls.append((symbol, tf))
        return bars(1400) if tf == "15m" else []

    scanner = Scanner(fetch, lambda: SYMBOLS[:1], DecisionEngine(), pace_seconds=0)
    bar = datetime(2026, 11, 2, 4, 30)
    assert await scanner.scan_if_due(bar + timedelta(seconds=40)) is True
    assert await scanner.scan_if_due(bar + timedelta(minutes=5)) is False
    assert await scanner.scan_if_due(bar + timedelta(minutes=15, seconds=40)) is True
    # 15m and 1h, once per scan, for the one symbol.
    assert calls == [("NSE:RELIANCE-EQ", "15m"), ("NSE:RELIANCE-EQ", "1h")] * 2


async def test_when_every_symbol_fails_it_retries_after_a_pause():
    failing = {"on": True}

    async def fetch(_symbol, tf):
        if failing["on"]:
            raise ConnectionError("token expired")
        return bars(1400) if tf == "15m" else []

    scanner = Scanner(fetch, lambda: SYMBOLS[:1], DecisionEngine(), pace_seconds=0)
    t = datetime(2026, 11, 2, 4, 30, 40)
    assert await scanner.scan_if_due(t) is True
    assert scanner.results[0]["error"] == "token expired"
    failing["on"] = False
    # Not hammering the broker: same bar, before the pause is over.
    assert await scanner.scan_if_due(t + timedelta(seconds=30)) is False
    assert await scanner.scan_if_due(t + RETRY_AFTER) is True
    assert scanner.results[0]["error"] is None
    assert await scanner.scan_if_due(t + RETRY_AFTER + timedelta(seconds=30)) is False


def test_the_bot_scans_its_watchlist_and_the_api_serves_the_ranking(client):
    from main import app

    bot = app.state.bot
    symbols = [s for s, _name, _kind in bot._scan_symbols()]
    assert "NSE:RELIANCE-EQ" in symbols and "NSE:NIFTY50-INDEX" in symbols
    assert bot.scanner._engine is bot.decision_engine

    # The engine runs the scan loop while it runs (idle in tests).
    assert (bot._scan_task is not None) == bot.is_running

    body = client.get("/api/scanner").json()
    assert body == {"scanned_at": None, "scanning": False, "timeframe": "15m",
                    "threshold": 55, "results": [], "bot_running": bot.is_running}


# ── Manual refresh (the dashboard's refresh button) ──────────────

def test_a_refresh_is_queued_once_then_waits_out_the_cooldown():
    scanner = Scanner(fake_history({}), lambda: [], DecisionEngine(), pace_seconds=0)
    t = datetime(2026, 11, 2, 5, 0)
    assert scanner.request_scan(t) == ("queued", 0.0)
    # Queued, not yet run: shown as scanning, and a second click is refused.
    assert scanner.snapshot()["scanning"] is True
    assert scanner.request_scan(t) == ("scanning", 0.0)
    scanner._manual_requested = False  # as the run loop takes it
    state, left = scanner.request_scan(t + timedelta(seconds=20))
    assert state == "cooldown" and left == (MANUAL_COOLDOWN - timedelta(seconds=20)).total_seconds()
    assert scanner.request_scan(t + MANUAL_COOLDOWN) == ("queued", 0.0)


def test_a_refresh_is_refused_while_a_scan_runs():
    scanner = Scanner(fake_history({}), lambda: [], DecisionEngine(), pace_seconds=0)
    scanner.scanning = True
    assert scanner.request_scan() == ("scanning", 0.0)


async def test_the_run_loop_scans_at_once_on_a_refresh_and_keeps_the_schedule(monkeypatch):
    # The conftest idles Scanner.run; this test runs the real loop.
    monkeypatch.setattr(scanner_module.Scanner, "run", _REAL_RUN)
    monkeypatch.setattr(scanner_module, "CHECK_SECONDS", 3600.0)
    calls = []

    async def fetch(symbol, tf):
        calls.append(tf)
        return bars(1400) if tf == "15m" else []

    scanner = Scanner(fetch, lambda: SYMBOLS[:1], DecisionEngine(), pace_seconds=0)
    task = asyncio.create_task(scanner.run())
    try:
        # The first pass is the scheduled one (never scanned yet).
        for _ in range(100):
            if scanner.scanned_at is not None and not scanner.scanning:
                break
            await asyncio.sleep(0.01)
        first, key = scanner.scanned_at, scanner._last_key
        assert len(calls) == 2
        # The loop now sleeps for CHECK_SECONDS (an hour): only the refresh
        # can wake it.
        await asyncio.sleep(0.05)
        assert scanner.request_scan()[0] == "queued"
        for _ in range(100):
            if len(calls) == 4 and not scanner.snapshot()["scanning"]:
                break
            await asyncio.sleep(0.01)
        assert len(calls) == 4
        assert scanner.scanned_at > first
        # A refresh does not move the schedule: the bar already scanned
        # stays the last scheduled one.
        assert scanner._last_key == key
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def test_refresh_needs_a_signed_in_user(db_path):
    from fastapi.testclient import TestClient

    import main

    with TestClient(main.app) as anonymous:
        response = anonymous.post("/api/scanner/refresh")
    assert response.status_code in (401, 403)


def test_refresh_queues_a_scan_and_never_touches_the_trade(client, monkeypatch):
    import main
    from tests.test_api_symbols import _open_trade

    bot = main.app.state.bot
    monkeypatch.setattr(bot, "is_running", True)
    symbol, version = bot.symbol, bot.symbol_version
    trade = _open_trade(bot.symbol)
    bot.active_trade = trade
    before = trade.model_dump()
    try:
        response = client.post("/api/scanner/refresh")
        assert response.status_code == 202
        body = response.json()
        assert body["message"] == "Scanning the watchlist" and body["scanning"] is True
        assert client.get("/api/scanner").json()["scanning"] is True

        again = client.post("/api/scanner/refresh")
        assert (again.status_code, again.json()["detail"]) == (409, "A scan is already running")

        bot.scanner._manual_requested = False  # as the run loop takes it
        soon = client.post("/api/scanner/refresh")
        assert soon.status_code == 429
        assert soon.json()["detail"].startswith("The watchlist was just rescanned: try again in ")
        assert int(soon.headers["Retry-After"]) > 0

        # The trade, the active symbol and the gate are as they were.
        assert bot.active_trade is trade and trade.model_dump() == before
        assert (bot.symbol, bot.symbol_version) == (symbol, version)
    finally:
        bot.active_trade = None


def test_refresh_is_refused_while_the_bot_is_stopped(client, monkeypatch):
    import main

    monkeypatch.setattr(main.app.state.bot, "is_running", False)
    response = client.post("/api/scanner/refresh")
    assert response.status_code == 409
    assert response.json()["detail"] == "The scanner runs while the bot runs: start the bot"


async def test_a_scan_with_an_open_trade_changes_nothing_the_bot_trades_on(client):
    """The scanner only reads history: scoring the traded symbol itself
    leaves the bot's trade, symbol, buffers and risk counters alone."""
    import main
    from tests.test_api_symbols import _open_trade

    bot = main.app.state.bot
    trade = _open_trade(bot.symbol)
    bot.active_trade = trade
    before = (
        trade.model_dump(), bot.symbol, bot.symbol_version,
        len(bot.analyzer.candles_15m), len(bot.analyzer.candles_1h),
        bot.risk_manager.to_state(),
    )
    scan = Scanner(
        fake_history({bot.symbol: bars(1400, step=0.5)}),
        lambda: [(bot.symbol, bot.symbol_name, "EQUITY")],
        bot.decision_engine, pace_seconds=0,
    )
    try:
        assert await scan.scan(NOW) is True
        assert scan.results[0]["error"] is None
        after = (
            trade.model_dump(), bot.symbol, bot.symbol_version,
            len(bot.analyzer.candles_15m), len(bot.analyzer.candles_1h),
            bot.risk_manager.to_state(),
        )
        assert after == before and bot.active_trade is trade
    finally:
        bot.active_trade = None
