"""The watchlist scanner: the live engine's verdict on every symbol's newest
closed 15m candle, ranked, scanned once per closed bar."""
from datetime import datetime, timedelta

from backtest.data import synthetic
from backtest.replay import OfflineAnalyzer, minute_ticks
from decision_engine import DecisionEngine
from models.candle import Candle
from scanner import RETRY_AFTER, Scanner, scan_key
from utils.live_bars import in_session

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

            async def fetch(_symbol, tf, _limit, history=history):
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
    async def fetch(symbol, tf, _limit):
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

    async def fetch(symbol, tf, _limit):
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

    async def fetch(_symbol, tf, _limit):
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
