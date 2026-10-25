"""Backtester: replays history through the live decision code, faithfully."""
from datetime import datetime, timedelta

import pytest

import trade_rules
from analyzer import Analyzer
from backtest import data
from backtest.costs import FUTURES, NONE, Slippage, preset_for
from backtest.replay import DecisionPoint, Replay, Tick, minute_ticks, replay
from backtest.report import split, stats
from backtest.simulator import COMPONENTS, Settings, Variant, simulate
from decision_engine import DecisionEngine
from models.candle import Candle
from models.enums import Direction, TradeStatus
from models.trade import Trade

# Monday 2026-10-26, the 10:00-10:15 IST bar (04:30 UTC); it closes on the
# first tick of the next bar, 04:45 UTC.
BAR = datetime(2026, 10, 26, 4, 30)
CLOSE_TICK = datetime(2026, 10, 26, 4, 45)


def long_setup() -> Candle:
    """A closed 15m candle the engine scores LONG 80: stop 23991, T1 24020."""
    return Candle(
        timestamp=BAR, open=23995, high=24001, low=23999, close=24000, volume=0,
        ema_20=23990, ema_50=23980, ema_200=23900, vwap=23950, rsi=55,
        macd_hist=1.5, atr=10,
    )


def decision(candle: Candle, patterns=("hammer",)) -> DecisionPoint:
    return DecisionPoint(0, candle, {
        "candle": candle, "patterns": list(patterns), "divergence": None,
        "fvg": None, "liquidity_sweep": None, "higher_timeframe_trend": Direction.WAIT,
    })


def path(*prices, start=CLOSE_TICK) -> list:
    return [Tick(start + timedelta(seconds=15 * i), p) for i, p in enumerate(prices)]


def exact(**kw) -> Settings:
    """No charges, no slippage, ₹1 lakh, single units."""
    return Settings(symbol="NSE:NIFTY50-INDEX", capital=100_000, lot_size=1,
                    costs=NONE, slippage=Slippage(0), **kw)


# ── Costs ─────────────────────────────────────────────────────────

def test_futures_charges_per_order():
    # One lot of 50 at 24,000 = ₹12 lakh turnover.
    sell = FUTURES.order_cost(-1, 24000, 50)
    buy = FUTURES.order_cost(1, 24000, 50)
    # 20 brokerage + 20.76 exchange + 1.20 SEBI + 18% GST on those three
    # + 240 STT (sell) / 24 stamp duty (buy).
    assert sell == pytest.approx(20 + 20.76 + 1.2 + 0.18 * 41.96 + 240)
    assert buy == pytest.approx(20 + 20.76 + 1.2 + 0.18 * 41.96 + 24)


def test_equity_brokerage_is_capped_by_turnover():
    eq = preset_for("NSE:SBIN-EQ")
    # 0.03% of ₹10,000 = ₹3 < ₹20 flat.
    assert eq.order_cost(1, 100, 100) == pytest.approx(3 + 0.297 + 0.01 + 0.18 * 3.307 + 0.3)
    assert preset_for("NSE:NIFTY50-INDEX") is FUTURES


def test_slippage_is_always_adverse():
    s = Slippage(0.01)
    assert s.fill(24000, 1) == 24002.4
    assert s.fill(24000, -1) == 23997.6


# ── Replay ────────────────────────────────────────────────────────

def test_a_minute_becomes_four_ticks_along_its_likely_path():
    up = Candle(timestamp=BAR, open=10, high=12, low=9, close=11, volume=0)
    down = Candle(timestamp=BAR, open=10, high=12, low=9, close=9.5, volume=0)
    assert [t.price for t in minute_ticks(up)] == [10, 9, 12, 11]
    assert [t.price for t in minute_ticks(down)] == [10, 12, 9, 9.5]
    assert [t.time for t in minute_ticks(up)] == [BAR + timedelta(seconds=s) for s in (0, 15, 30, 45)]


async def test_replay_builds_the_same_15m_candles_as_the_exchange():
    minutes = data.synthetic(2, volume=100)
    rep = await replay(minutes)
    # 25 bars a day; the last bar of the data never closes.
    assert len(rep.decisions) == 2 * 25 - 1
    first = rep.decisions[0]
    group = minutes[:15]
    assert (first.candle.open, first.candle.close) == (group[0].open, group[-1].close)
    assert first.candle.high == max(c.high for c in group)
    assert first.candle.low == min(c.low for c in group)
    assert first.candle.volume == 1500
    # Decided on the first tick of the next bar, like the live engine.
    assert rep.ticks[first.tick_index].time == minutes[15].timestamp


async def test_replay_signals_match_the_live_engine(db_path, monkeypatch):
    """The same prices through NiftyBot._on_tick give the same signals."""
    from bot import NiftyBot
    from database import init_db

    await init_db()
    minutes = data.synthetic(4)
    rep = await replay(minutes, max_bars=10**9)
    engine = DecisionEngine()
    expected = [engine.evaluate(p.analysis) for p in rep.decisions]

    async def _offline_context(self):
        from backtest.replay import OFFLINE_CONTEXT
        return dict(OFFLINE_CONTEXT)

    async def _open(*args, **kwargs):
        return True, "OK"

    monkeypatch.setattr(Analyzer, "_get_market_context", _offline_context)
    bot = NiftyBot()
    bot.is_running = True
    bot.analyzer_symbol = bot.symbol
    bot.risk_manager.can_trade = _open
    seen = []

    async def _capture(message):
        if message["type"] == "signal":
            seen.append(message["signal"])

    bot._broadcast = _capture
    epoch = datetime(1970, 1, 1)
    for tick in rep.ticks:
        await bot._on_tick({
            "symbol": bot.symbol, "ltp": tick.price, "v": 0,
            "exch_time": int((tick.time - epoch).total_seconds()),
        })
    bot.is_running = False

    assert len(seen) == len(expected) > 50
    for live, replayed in zip(seen, expected):
        assert live["direction"] == replayed.direction
        assert live["score"] == replayed.score
        assert live["stop_loss"] == replayed.stop_loss
        assert live["target_1"] == replayed.target_1


async def test_patterns_read_the_closed_candle_not_the_new_one():
    analyzer = Analyzer()

    async def _ctx():
        return {}

    analyzer._get_market_context = _ctx
    # A bearish bar, then a hammer (long lower wick) that closes...
    analyzer.candles_15m = [
        Candle(timestamp=BAR - timedelta(minutes=15), open=110, high=111, low=99, close=100, volume=0),
        Candle(timestamp=BAR, open=100, high=101.5, low=90, close=101, volume=0),
    ]
    hammer = analyzer.candles_15m[-1]
    # ...when the next bar's first tick arrives (a one-tick bar).
    analyzer.candles_15m.append(
        Candle(timestamp=BAR + timedelta(minutes=15), open=101, high=101, low=101, close=101, volume=0)
    )
    analysis = await analyzer.analyze(hammer)
    assert "hammer" in analysis["patterns"]


# ── Exit rules ────────────────────────────────────────────────────

def make_trade(**kw) -> Trade:
    fields = dict(entry_time=CLOSE_TICK, direction=Direction.LONG, entry_price=24000,
                  quantity=144, initial_qty=144, remaining_qty=144, stop_loss=23991,
                  target_1=24020, target_2=24030, target_3=24040, signal_score=80,
                  confidence=0.8)
    fields.update(kw)
    return Trade(**fields)


def test_exit_rule_priority():
    t = make_trade()
    assert trade_rules.next_exit(t, 24005, CLOSE_TICK) is None
    assert trade_rules.next_exit(t, 24020, CLOSE_TICK).reason == "TARGET_1"
    assert trade_rules.next_exit(t, 23990, CLOSE_TICK).reason == "STOP_LOSS"
    assert trade_rules.next_exit(t, 23990, CLOSE_TICK).price == 23990
    # 15:15 IST beats everything.
    eod = trade_rules.next_exit(t, 24020, datetime(2026, 10, 26, 9, 45))
    assert (eod.reason, eod.fraction) == ("EOD_SQUARE_OFF", None)
    t.status = TradeStatus.PARTIAL_1
    assert trade_rules.next_exit(t, 24020, CLOSE_TICK) is None
    assert trade_rules.next_exit(t, 24030, CLOSE_TICK).new_status == TradeStatus.PARTIAL_2


def test_partial_quantities_are_whole_lots():
    t = make_trade(initial_qty=200, remaining_qty=200)
    assert trade_rules.partial_qty(t, 0.5, 50) == 100
    assert trade_rules.partial_qty(t, 0.375, 50) == 50
    assert trade_rules.partial_qty(t, 0.375, 1) == 75


# ── Simulation ────────────────────────────────────────────────────

async def test_a_trade_runs_through_all_its_exits():
    rep = Replay(path(24000, 24010, 24020, 24030, 23990), [decision(long_setup())])
    result = await simulate(rep, Variant(), exact())
    [record] = result.trades
    t = record.trade
    # ₹1,000 risk x (0.5 + 0.8 confidence) / 9 points = 144 units.
    assert (t.entry_price, t.initial_qty, t.stop_loss) == (24000, 144, 23991)
    assert (t.exit_t1_qty, t.exit_t2_qty) == (72, 54)
    assert record.exit_reason == "STOP_LOSS" and t.exit_price == 23990
    assert record.gross == 72 * 20 + 54 * 30 - 18 * 10
    assert record.charges == 0


async def test_charges_and_slippage_are_paid_on_every_fill():
    # One exit step per tick, as in the live engine: T1, T2, then T3.
    rep = Replay(path(24000, 24040, 24040, 24040), [decision(long_setup())])
    config = exact()
    config.costs = FUTURES
    config.slippage = Slippage(0.01)
    [record] = (await simulate(rep, Variant(), config)).trades
    t = record.trade
    assert t.entry_price == 24002.4  # bought above the price
    assert t.exit_t1_price == 24017.6  # sold below T1
    orders = [(1, 24002.4, 144), (-1, 24017.6, 72), (-1, 24027.6, 54), (-1, 24037.6, 18)]
    assert record.exit_reason == "TARGET_3"
    assert record.charges == pytest.approx(sum(FUTURES.order_cost(*o) for o in orders))
    assert record.net == pytest.approx(record.gross - record.charges)


async def test_an_open_trade_is_closed_at_the_end_of_the_data():
    rep = Replay(path(24000, 24005), [decision(long_setup())])
    [record] = (await simulate(rep, Variant(), exact())).trades
    assert record.exit_reason == "END_OF_DATA" and record.gross == 144 * 5


async def test_the_live_risk_rules_apply():
    # Two decisions while the first trade is still open: one position only.
    ticks = path(24000, 24005, 24006, 24007)
    second = DecisionPoint(2, long_setup(), decision(long_setup()).analysis)
    result = await simulate(Replay(ticks, [decision(long_setup()), second]), Variant(), exact())
    assert len(result.trades) == 1
    assert result.skipped == {"Trade already open": 1}


async def test_trading_waits_for_the_warm_up():
    rep = Replay(path(24000, 24040), [decision(long_setup())])
    result = await simulate(rep, Variant(), exact(trade_from=CLOSE_TICK + timedelta(minutes=1)))
    assert result.trades == []


async def test_a_lot_too_big_for_the_capital_is_not_taken():
    rep = Replay(path(24000, 24040), [decision(long_setup())])
    config = exact()
    config.lot_size = 200  # 200 x 9 points = ₹1,800 > ₹1,300 allowed
    result = await simulate(rep, Variant(), config)
    assert result.trades == [] and result.skipped == {"Position size 0": 1}


async def test_switching_off_an_ingredient():
    rep = Replay(path(24000, 24040), [decision(long_setup())])
    # 80 - 15 (pattern) - 25 (trend) = 40 < 55: no trade without both.
    both = Variant("no_both", frozenset({"patterns", "trend"}))
    assert (await simulate(rep, both, exact())).trades == []
    # The candle the live code produced is never modified.
    assert rep.decisions[0].candle.ema_20 == 23990
    assert len((await simulate(rep, Variant.without("patterns"), exact())).trades) == 1
    with pytest.raises(ValueError):
        Variant.without("astrology")
    assert set(COMPONENTS) >= {"trend", "vwap", "rsi", "macd", "patterns", "ht_filter"}


# ── Report ────────────────────────────────────────────────────────

async def test_stats():
    class R:
        def __init__(self, net, at):
            self.net, self.gross, self.charges, self.r_multiple = net, net, 0.0, net / 100
            self.trade = make_trade(entry_time=at)

    rs = [R(300, BAR), R(-100, BAR), R(-200, BAR + timedelta(days=1)), R(100, BAR + timedelta(days=2))]
    s = stats(rs)
    assert (s.trades, s.wins, s.net) == (4, 2, 100)
    assert s.profit_factor == pytest.approx(400 / 300)
    assert s.max_drawdown == 300  # +300 peak, then -100, -200
    first, second = split(rs, BAR + timedelta(hours=12))
    assert (len(first), len(second)) == (2, 2)


async def test_command_line_on_synthetic_prices(capsys, tmp_path, monkeypatch):
    import backtest.__main__ as cli

    monkeypatch.setattr(cli, "RESULTS_DIR", tmp_path)
    args = cli._parser().parse_args(["run", "--synthetic", "12", "--capital", "1000000"])
    assert await cli._main(args) == 0
    out = capsys.readouterr().out
    assert "Backtest baseline" in out and "trades taken" in out
    assert list(tmp_path.glob("trades_*.csv"))
