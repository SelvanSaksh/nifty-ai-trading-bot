"""Market structure: swings, HH/HL/LH/LL, BOS/CHoCH, zones, trend."""
import random
from datetime import datetime, timedelta

import pytest

from config import settings
from utils.market_structure import (
    StructureTracker,
    analyze_structure,
    bar_end,
    closed_candles,
)

START = datetime(2026, 10, 12, 3, 45)  # 09:15 IST
STEP = timedelta(minutes=5)


def bars_from_waypoints(points, leg=8):
    """Straight legs between waypoints; one bar per step, no wicks."""
    closes = [points[0]]
    for a, b in zip(points, points[1:]):
        for i in range(1, leg + 1):
            closes.append(a + (b - a) * i / leg)
    bars = []
    prev = closes[0]
    for i, close in enumerate(closes):
        bars.append(
            {
                "timestamp": START + STEP * i,
                "open": prev,
                "high": max(prev, close),
                "low": min(prev, close),
                "close": close,
            }
        )
        prev = close
    return bars


def labels(result):
    return [(s.kind, s.price, s.label) for s in result.swings]


def events(result):
    return [(b.type, b.direction, b.price) for b in result.breaks]


class TestSequences:
    def test_uptrend_is_hh_hl_and_continuation_is_bos(self):
        result = analyze_structure(bars_from_waypoints([100, 120, 110, 130, 120, 140, 130]))
        assert labels(result) == [
            ("high", 120, None),
            ("low", 110, None),
            ("high", 130, "HH"),
            ("low", 120, "HL"),
            ("high", 140, "HH"),
        ]
        assert result.trend == "up" and result.trend_confirmed
        assert result.sequence == "up"
        # 120 broke while there was no trend yet: no event. 130 is a BOS.
        assert events(result) == [("BOS", "up", 130)]

    def test_downtrend_is_lh_ll_and_continuation_is_bos(self):
        result = analyze_structure(bars_from_waypoints([200, 180, 190, 170, 180, 160, 170]))
        assert [l for _, _, l in labels(result)] == [None, None, "LL", "LH", "LL"]
        assert result.trend == "down" and result.trend_confirmed
        assert events(result) == [("BOS", "down", 170)]

    def test_close_below_the_last_hl_is_choch_then_lh_confirms(self):
        #       HH 130 ... HL 120 broken by a close at 110, then LH 125.
        result = analyze_structure(
            bars_from_waypoints([100, 120, 110, 130, 120, 140, 125, 135, 105, 125, 100])
        )
        assert ("CHoCH", "down", 125) in events(result)
        choch = next(b for b in result.breaks if b.type == "CHoCH")
        assert choch.swing_label == "HL"
        assert result.trend == "down"
        assert result.trend_confirmed, "the LH after the CHoCH confirms the downtrend"
        assert labels(result)[-1] == ("high", 125, "LH")

    def test_choch_is_unconfirmed_until_the_lh(self):
        bars = bars_from_waypoints([100, 120, 110, 130, 120, 140, 125, 135, 105])
        result = analyze_structure(bars)
        assert events(result)[-1] == ("CHoCH", "down", 125)
        assert result.trend == "down" and not result.trend_confirmed

    def test_a_swing_from_before_the_choch_does_not_confirm_it(self):
        # The 135 LH peaked before the break and is confirmed only after it.
        result = analyze_structure(
            bars_from_waypoints([100, 120, 110, 130, 120, 140, 125, 135, 105])
        )
        assert ("high", 135, "LH") in labels(result)
        assert not result.trend_confirmed

    def test_a_failed_choch_leaves_no_trend(self):
        # CHoCH down at 125; the next swing high pokes a wick above 135 (HH)
        # without closing above it, so it is no LH: the CHoCH failed.
        bars = bars_from_waypoints([100, 120, 110, 130, 120, 140, 125, 135, 105, 134, 110])
        peak = max(range(len(bars) - 10, len(bars)), key=lambda i: bars[i]["high"])
        bars[peak]["high"] = 137
        result = analyze_structure(bars)
        assert ("CHoCH", "down", 125) in events(result)
        assert ("high", 137, "HH") in labels(result)
        assert result.trend == "none"

    def test_mixed_sequence_is_no_trend_and_breaks_are_not_labelled(self):
        # HH then LL (by a lower swing low) -> mixed -> no trend.
        bars = bars_from_waypoints([100, 120, 110, 130, 120, 140, 115, 125])
        # Make the 115 trough a wick only, so no candle *closes* below HL 120.
        trough = min(range(len(bars)), key=lambda i: bars[i]["low"])
        for i in range(len(bars)):
            if bars[i]["close"] < 120:
                bars[i]["close"] = 120.5
            bars[i]["open"] = max(bars[i]["open"], 120.5)
        bars[trough]["low"] = 115
        result = analyze_structure(bars)
        assert ("low", 115, "LL") in labels(result)
        assert result.sequence == "mixed"
        assert result.trend == "none"
        assert all(b.type != "CHoCH" for b in result.breaks), "wicks never break structure"

    def test_a_trend_born_on_the_break_candle_gets_no_event(self):
        # Short legs: the candle that confirms HL 120 (3 bars after the low)
        # is the same one that closes above 130. While it was forming there
        # was no trend, so the chart previewed nothing - and the bot agrees.
        result = analyze_structure(bars_from_waypoints([100, 120, 110, 130, 120, 140, 130], leg=5))
        assert result.trend == "up"
        assert all(b.price != 130 for b in result.breaks)

    def test_first_swings_have_no_label(self):
        result = analyze_structure(bars_from_waypoints([100, 120, 110]))
        assert labels(result)[0][2] is None


class TestClosesOnly:
    def test_a_wick_above_the_high_is_not_a_bos(self):
        bars = bars_from_waypoints([100, 120, 110, 130, 120, 128, 121])
        # An uptrend exists (HH 130 / HL 120). Poke a wick through 130.
        poke = max(range(len(bars) - 3, len(bars)), key=lambda i: bars[i]["high"])
        bars[poke]["high"] = 133
        result = analyze_structure(bars)
        assert all(b.price != 130 for b in result.breaks)

    def test_break_candle_closes_beyond_the_level(self):
        result = analyze_structure(bars_from_waypoints([100, 120, 110, 130, 120, 140, 130]))
        bos = result.breaks[0]
        assert bos.close > bos.price


class TestZones:
    def test_unbroken_swings_leave_zones_and_broken_ones_are_dropped(self):
        result = analyze_structure(bars_from_waypoints([100, 120, 110, 130, 120, 140, 130]))
        resistance = [z for z in result.zones if z.kind == "resistance"]
        # 120 and 130 were closed above; only 140 remains as resistance.
        assert [z.top for z in resistance] == [140]
        assert all(z.bottom <= z.top for z in result.zones)


class TestWatch:
    def test_uptrend_previews_bos_up_and_choch_down(self):
        result = analyze_structure(bars_from_waypoints([100, 120, 110, 130, 120, 140, 128, 135]))
        watch = result.watch()
        assert watch["up"]["price"] == 140 and watch["up"]["event"] == "BOS"
        assert watch["down"]["price"] == 128 and watch["down"]["event"] == "CHoCH"

    def test_no_trend_previews_nothing(self):
        result = analyze_structure(bars_from_waypoints([100, 120, 110, 115]))
        assert result.trend == "none"
        for level in result.watch().values():
            assert level is None or level["event"] is None

    def test_a_broken_level_is_not_watched_again(self):
        result = analyze_structure(bars_from_waypoints([100, 120, 110, 130, 120, 140, 130, 145]))
        assert result.last_high is None  # 140 was closed above
        assert result.watch()["up"] is None


class TestClosedCandles:
    def test_forming_candle_is_dropped(self):
        bars = bars_from_waypoints([100, 110], leg=4)
        now = bars[-1]["timestamp"] + timedelta(minutes=2)
        assert closed_candles(bars, 5, now) == bars[:-1]
        assert closed_candles(bars, 5, now + timedelta(minutes=3)) == bars

    def test_last_hour_bar_closes_at_the_session_close(self):
        start = datetime(2026, 10, 12, 9, 45)  # 15:15 IST
        assert bar_end(start, 60) == datetime(2026, 10, 12, 10, 0)
        assert bar_end(datetime(2026, 10, 12, 8, 45), 60) == datetime(2026, 10, 12, 9, 45)


# ── properties over random markets ──────────────────────────────

def random_bars(seed, count=300):
    rng = random.Random(seed)
    price = 25000.0
    bars = []
    for i in range(count):
        open_ = price
        close = open_ + rng.gauss(0, 15)
        high = max(open_, close) + abs(rng.gauss(0, 6))
        low = min(open_, close) - abs(rng.gauss(0, 6))
        # Real feeds repeat prices; make ties happen.
        high, low, close = round(high), round(low), round(close)
        bars.append({"timestamp": START + STEP * i, "open": open_, "high": high, "low": low, "close": close})
        price = close
    return bars


def frozen(result):
    swings = [(s.kind, s.time, s.price, s.label, s.confirmed_time, s.derived) for s in result.swings]
    breaks = [(b.type, b.direction, b.price, b.time) for b in result.breaks]
    return swings, breaks


SEEDS = range(40)


@pytest.mark.parametrize("seed", SEEDS)
def test_confirmed_structure_never_changes(seed):
    bars = random_bars(seed)
    full_swings, full_breaks = frozen(analyze_structure(bars))
    for cut in range(1, len(bars), 7):
        swings, breaks = frozen(analyze_structure(bars[:cut]))
        assert full_swings[: len(swings)] == swings
        assert full_breaks[: len(breaks)] == breaks


@pytest.mark.parametrize("seed", SEEDS)
def test_swings_alternate_and_labels_compare_like_with_like(seed):
    result = analyze_structure(random_bars(seed))
    swings = result.swings
    for a, b in zip(swings, swings[1:]):
        assert a.kind != b.kind
        assert a.index < b.index or (a.index == b.index)
    for kind, up, down, eq in (("high", "HH", "LH", "EQH"), ("low", "HL", "LL", "EQL")):
        same = [s for s in swings if s.kind == kind]
        assert same[0].label is None if same else True
        for prev, cur in zip(same, same[1:]):
            expected = up if cur.price > prev.price else down if cur.price < prev.price else eq
            assert cur.label == expected


@pytest.mark.parametrize("seed", SEEDS)
def test_every_label_is_confirmed_late_and_from_closed_data(seed):
    bars = random_bars(seed)
    result = analyze_structure(bars, strength=3)
    for s in result.swings:
        assert s.confirmed_index >= s.index + (0 if s.derived else 3)
        extreme = bars[s.index]["high"] if s.kind == "high" else bars[s.index]["low"]
        assert s.price == extreme


@pytest.mark.parametrize("seed", SEEDS)
def test_breaks_are_closes_beyond_a_level_confirmed_before_them(seed):
    bars = random_bars(seed)
    result = analyze_structure(bars)
    confirmed_at = {(s.time, s.price): s.confirmed_index for s in result.swings}
    for b in result.breaks:
        close = bars[b.index]["close"]
        assert close > b.price if b.direction == "up" else close < b.price
        assert confirmed_at[(b.swing_time, b.price)] <= b.index
        if b.type == "BOS":
            assert b.swing_label in ("HH", "HL", "LH", "LL", "EQH", "EQL", None)


@pytest.mark.parametrize("seed", SEEDS)
def test_the_live_preview_always_matches_what_the_close_produces(seed):
    """What the chart previews for the forming candle (``watch``) is exactly
    what the bot records once that candle closes - never anything else."""
    bars = random_bars(seed, count=200)
    for m in range(1, len(bars)):
        before = analyze_structure(bars[:m])
        after = analyze_structure(bars[: m + 1])
        close = bars[m]["close"]
        expected = []
        watch = before.watch()
        if watch["up"] and close > watch["up"]["price"] and watch["up"]["event"]:
            expected.append((watch["up"]["event"], "up", watch["up"]["price"]))
        if watch["down"] and close < watch["down"]["price"] and watch["down"]["event"]:
            expected.append((watch["down"]["event"], "down", watch["down"]["price"]))
        new = [(b.type, b.direction, b.price) for b in after.breaks[len(before.breaks):]]
        assert new == expected, f"bar {m}"


@pytest.mark.parametrize("seed", SEEDS)
def test_no_event_is_recorded_without_a_trend(seed):
    bars = random_bars(seed, count=200)
    full = analyze_structure(bars)
    for b in full.breaks:
        trend_before = analyze_structure(bars[: b.index]).trend
        assert trend_before != "none"
        assert b.type == ("BOS" if trend_before == b.direction else "CHoCH")


def test_strength_must_be_positive():
    with pytest.raises(ValueError):
        analyze_structure([], strength=0)


def test_empty_series():
    result = analyze_structure([])
    assert result.swings == [] and result.trend == "none"
    assert result.to_dict()["watch"] == {"up": None, "down": None}


# ── incremental tracker ─────────────────────────────────────────

@pytest.mark.parametrize("seed", range(15))
def test_feeding_in_batches_equals_analysing_everything(seed):
    rng = random.Random(seed)
    bars = random_bars(seed, count=400)
    tracker = StructureTracker()
    i = 0
    while i < len(bars):
        size = rng.randint(1, 40)
        # Overlapping batches, like repeated history requests.
        tracker.feed(bars[max(0, i - rng.randint(0, 5)) : i + size])
        i += size
    assert tracker.result().to_dict() == analyze_structure(bars).to_dict()


# ── API ─────────────────────────────────────────────────────────

@pytest.fixture
def recent_start(monkeypatch):
    # Keep the mock history small: start the structure a few days back.
    start = (datetime.utcnow() - timedelta(days=6)).strftime("%Y-%m-%d")
    monkeypatch.setattr(settings, "STRUCTURE_START", start)
    return start


def test_structure_endpoint_returns_confirmed_structure(client, recent_start):
    resp = client.get(
        "/api/structure",
        params={"symbol": "NSE:NIFTY50-INDEX", "timeframe": "5m"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["error"] is None
    assert body["symbol"] == "NSE:NIFTY50-INDEX" and body["timeframe"] == "5m"
    assert body["start"].startswith(recent_start)
    # No 200-candle cap: every session bar since the start is used.
    assert body["candles_used"] > 200
    assert body["trend"] in ("up", "down", "none")
    assert body["sequence"] in ("up", "down", "mixed", "none")
    assert set(body["watch"]) == {"up", "down"}
    assert body["swings"], "a few days of 5m bars always contain swings"
    for swing in body["swings"]:
        assert swing["kind"] in ("high", "low")
        assert swing["confirmed_time"] >= swing["time"]
    for b in body["breaks"]:
        assert b["type"] in ("BOS", "CHoCH")


def test_since_only_trims_what_is_returned(client, recent_start):
    params = {"symbol": "NSE:NIFTY50-INDEX", "timeframe": "5m"}
    full = client.get("/api/structure", params=params).json()
    cut = full["swings"][len(full["swings"]) // 2]["time"]
    part = client.get("/api/structure", params={**params, "since": cut}).json()
    assert part["swings"] == [s for s in full["swings"] if s["time"] >= cut]
    assert part["breaks"] == [b for b in full["breaks"] if b["time"] >= cut]
    for key in ("trend", "trend_confirmed", "sequence", "watch", "zones", "candles_used"):
        assert part[key] == full[key]


def test_structure_endpoint_validates_input(client, recent_start):
    bad_strength = client.get(
        "/api/structure",
        params={"symbol": "NSE:NIFTY50-INDEX", "timeframe": "5m", "strength": 0},
    )
    assert bad_strength.status_code == 422
    bad_tf = client.get(
        "/api/structure", params={"symbol": "NSE:NIFTY50-INDEX", "timeframe": "7m"}
    )
    assert bad_tf.status_code == 422
    bad_since = client.get(
        "/api/structure",
        params={"symbol": "NSE:NIFTY50-INDEX", "timeframe": "5m", "since": "yesterday"},
    )
    assert bad_since.status_code == 422


class FakeRangeBroker:
    """History endpoint stand-in that records every requested range."""

    def __init__(self, bars):
        self.bars = bars
        self.calls = []

    async def get_history_range(self, symbol, timeframe, start, end):
        self.calls.append((start, end))
        return [b for b in self.bars if start <= b["timestamp"] <= end]


def make_bot(monkeypatch, bars, start):
    from bot import NiftyBot

    bot = NiftyBot.__new__(NiftyBot)
    bot.broker = FakeRangeBroker(bars)
    bot._structure_cache = {}
    bot._structure_locks = {}
    bot.symbol = "NSE:NIFTY50-INDEX"
    bot.symbol_name = "NIFTY 50"
    monkeypatch.setattr(settings, "STRUCTURE_START", start)
    monkeypatch.setattr(NiftyBot, "STRUCTURE_REFRESH_SECONDS", 0.0)
    return bot


async def test_bot_reads_everything_once_then_only_new_candles(monkeypatch):
    bars = random_bars(5, count=900)  # far more than 200
    bot = make_bot(monkeypatch, bars, "2026-10-12")
    after = lambda m: bars[m - 1]["timestamp"] + timedelta(minutes=5)  # bar m-1 closed

    first = await bot.get_structure("NSE:NIFTY50-INDEX", "5m", now=after(600))
    assert first["candles_used"] == 600
    assert bot.broker.calls[0][0] == datetime(2026, 10, 12, 3, 45), "starts at the session open"

    second = await bot.get_structure("NSE:NIFTY50-INDEX", "5m", now=after(900))
    assert second["candles_used"] == 900
    assert bot.broker.calls[1][0] == bars[599]["timestamp"], "only new candles are fetched"
    assert {k: v for k, v in second.items() if k != "summary"} == {
        "symbol": "NSE:NIFTY50-INDEX",
        "symbol_name": second["symbol_name"],
        "timeframe": "5m",
        "start": "2026-10-12T03:45:00",
        "error": None,
        **analyze_structure(bars).to_dict(),
    }
    # What was confirmed with 600 candles is still there, unchanged.
    assert second["swings"][: len(first["swings"])] == [
        {**s, "broken_time": t["broken_time"]} for s, t in zip(first["swings"], second["swings"])
    ]
    assert second["breaks"][: len(first["breaks"])] == first["breaks"]


async def test_bot_leaves_out_the_forming_candle(monkeypatch):
    bars = random_bars(3, count=300)
    bot = make_bot(monkeypatch, bars, "2026-10-12")
    forming = bars[-1]["timestamp"] + timedelta(minutes=2)
    body = await bot.get_structure("NSE:NIFTY50-INDEX", "5m", now=forming)
    assert body["candles_used"] == len(bars) - 1
    assert body["last_closed_time"] == bars[-2]["timestamp"].isoformat()


async def test_a_failed_fetch_keeps_the_known_structure(monkeypatch):
    bars = random_bars(4, count=300)
    bot = make_bot(monkeypatch, bars, "2026-10-12")
    end = bars[-1]["timestamp"] + timedelta(minutes=5)
    good = await bot.get_structure("NSE:NIFTY50-INDEX", "5m", now=end)

    async def broken(*args, **kwargs):
        raise ConnectionError("Fyers down")

    bot.broker.get_history_range = broken
    again = await bot.get_structure("NSE:NIFTY50-INDEX", "5m", now=end)
    assert again["error"] == "Fyers down"
    assert again["swings"] == good["swings"] and again["trend"] == good["trend"]
