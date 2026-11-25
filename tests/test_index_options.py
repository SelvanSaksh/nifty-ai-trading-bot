"""Bank Nifty traded through its at-the-money CALL options.

The signal, stop and targets are Bank Nifty INDEX levels (checked against the
index price); what is bought and sold is the call, at its own quote, so the
entry/exit prices and the P&L are premiums. Calls only, paper only.
"""
import calendar
import os
from datetime import datetime, timedelta

import pytest

import trade_rules
from brokers.fyers_broker import parse_quote
from config import settings
from database import get_open_trade, init_db
from features.option_contracts import (
    OptionBook,
    OptionContractError,
    choose_atm,
    choose_expiry,
    describe,
    parse_master,
)
from models.enums import Direction, SignalResult, TradeStatus
from models.signal import Signal
from risk_manager import RiskManager
from tests.test_trading_safety import T0, FakeBroker

BANKNIFTY = "NSE:NIFTYBANK-INDEX"

# Expiries as the master file gives them (10:10 UTC = 15:40 IST). Jan 2027
# expires on Monday the 25th: Tuesday the 26th is Republic Day.
NOV_24 = datetime(2026, 11, 24, 10, 10)
DEC_29 = datetime(2026, 12, 29, 10, 10)
JAN_25 = datetime(2027, 1, 25, 10, 10)
MONTHS = {NOV_24: "26NOV", DEC_29: "26DEC", JAN_25: "27JAN"}


def row(expiry, strike, kind="CE", instrument="14", lot=30, underlying="BANKNIFTY"):
    """One NSE_FO.csv row, in the file's own column layout."""
    epoch = calendar.timegm(expiry.timetuple())
    tag = f"{strike}{kind}" if kind in ("CE", "PE") else "FUT"
    symbol = f"NSE:{underlying}{MONTHS[expiry]}{tag}"
    strike_field = f"{float(strike)}" if kind in ("CE", "PE") else "-1.0"
    return (
        f"1011261229350{strike % 100:02d},{underlying} {expiry:%d %b %y} {strike} {kind},"
        f"{instrument},{lot},0.05,,0915-1540|1815-1915:,2026-11-23,{epoch},{symbol},"
        f"10,11,35000,{underlying},26009,{strike_field},{kind},101000000026009,None,0,0.0"
    )


def master_text():
    rows = []
    for expiry in (NOV_24, DEC_29, JAN_25):
        for strike in (50900, 51000, 51100):
            rows.append(row(expiry, strike))
            rows.append(row(expiry, strike, kind="PE"))
    rows += [
        row(DEC_29, 0, kind="XX", instrument="11"),     # the future
        row(DEC_29, 51000, underlying="NIFTY"),         # another index
        "garbage,row",
        row(DEC_29, 51000).replace(",30,0.05,", ",abc,0.05,"),  # bad lot
    ]
    return "\n".join(rows) + "\n"


# ── The symbol master ────────────────────────────────────────────

def test_the_master_yields_only_bank_nifty_index_calls():
    calls = parse_master(master_text(), "BANKNIFTY", "CE")
    assert len(calls) == 9
    assert {c.option_type for c in calls} == {"CE"}
    assert {c.lot_size for c in calls} == {30} and {c.tick_size for c in calls} == {0.05}
    first = min(calls, key=lambda c: (c.expiry, c.strike))
    assert (first.symbol, first.strike, first.expiry) == (
        "NSE:BANKNIFTY26NOV50900CE", 50900.0, NOV_24,
    )
    assert describe(first) == "BANKNIFTY 24 Nov 2026 50900 CE (lot 30)"


def test_the_real_master_row_format_parses():
    # Copied from public.fyers.in/sym_details/NSE_FO.csv on 2026-11-25.
    real = ("101126122935000,BANKNIFTY 29 Dec 26 69400 CE,14,30,0.05,,0915-1540|1815-1915:,"
            "2026-11-23,1798539000,NSE:BANKNIFTY26DEC69400CE,10,11,35000,BANKNIFTY,26009,"
            "69400.0,CE,101000000026009,None,0,0.0\n")
    (contract,) = parse_master(real, "BANKNIFTY")
    assert contract.symbol == "NSE:BANKNIFTY26DEC69400CE"
    assert contract.expiry == DEC_29 and contract.strike == 69400.0 and contract.lot_size == 30


def test_the_nearest_expiry_at_least_two_days_away():
    calls = parse_master(master_text(), "BANKNIFTY")
    # 26 Oct: November is nearest.
    assert choose_expiry(calls, T0, 2) == NOV_24
    # Sunday 22 Nov (IST): the 24th is 2 days away, still allowed.
    assert choose_expiry(calls, datetime(2026, 11, 22, 5, 0), 2) == NOV_24
    # Monday 23 Nov: 1 day away -> December.
    assert choose_expiry(calls, datetime(2026, 11, 23, 5, 0), 2) == DEC_29
    # After December's expiry: January, which is a Monday (holiday move).
    assert choose_expiry(calls, datetime(2026, 12, 30, 5, 0), 2) == JAN_25
    with pytest.raises(OptionContractError, match="No expiry at least 2 day"):
        choose_expiry(calls, datetime(2027, 1, 24, 5, 0), 2)


@pytest.mark.parametrize("price,strike", [
    (50949.95, 50900.0), (50950.0, 50900.0), (50950.05, 51000.0),
    (51012.0, 51000.0), (51049.95, 51000.0), (51200.0, 51100.0),
])
def test_atm_is_the_nearest_listed_strike_lower_on_a_tie(price, strike):
    calls = parse_master(master_text(), "BANKNIFTY")
    assert choose_atm(calls, DEC_29, price).strike == strike


async def test_the_master_is_downloaded_once_a_day_and_kept_on_disk(tmp_path):
    fetches = []

    async def fetch():
        fetches.append(1)
        return master_text()

    path = tmp_path / "NSE_FO.csv"
    book = OptionBook(fetch, path)
    assert len(await book.contracts("BANKNIFTY", now_utc=T0)) == 9
    assert len(await book.contracts("BANKNIFTY", now_utc=T0 + timedelta(hours=3))) == 9
    assert len(fetches) == 1 and path.exists()

    # A restart the same IST day reads the saved copy.
    os.utime(path, (calendar.timegm(T0.timetuple()),) * 2)
    again = OptionBook(fetch, path)
    assert len(await again.contracts("BANKNIFTY", now_utc=T0)) == 9
    assert len(fetches) == 1

    # The next day the copy is stale: downloaded again.
    assert len(await again.contracts("BANKNIFTY", now_utc=T0 + timedelta(days=1))) == 9
    assert len(fetches) == 2


async def test_no_master_means_no_contract(tmp_path):
    async def down():
        raise ConnectionError("unreachable")

    with pytest.raises(OptionContractError, match="Symbol master download failed: unreachable"):
        await OptionBook(down, tmp_path / "m.csv").contracts("BANKNIFTY", now_utc=T0)

    async def empty():
        return "garbage\n"

    with pytest.raises(OptionContractError, match="lists no BANKNIFTY CE options"):
        await OptionBook(empty, tmp_path / "e.csv").contracts("BANKNIFTY", now_utc=T0)


# ── Quotes ───────────────────────────────────────────────────────

def test_a_fyers_quote_is_read_and_zero_prices_are_none():
    sym = "NSE:BANKNIFTY26DEC51000CE"
    reply = {"s": "ok", "d": [{"n": sym, "s": "ok", "v": {"lp": 612.4, "bid": 612.0, "ask": 612.9}}]}
    assert parse_quote(reply, sym) == {"ltp": 612.4, "bid": 612.0, "ask": 612.9}
    zeros = {"s": "ok", "d": [{"n": sym, "s": "ok", "v": {"lp": 0, "bid": 0, "ask": None}}]}
    assert parse_quote(zeros, sym) is None
    assert parse_quote(reply, "NSE:OTHER") is None
    assert parse_quote({"s": "ok", "d": [{"n": sym, "s": "error", "v": {}}]}, sym) is None


# ── Sizing: the worst case at the index stop ─────────────────────

def signal(stop=50862.0, t1=51237.0, t2=51387.0, t3=51537.0, direction=Direction.LONG, confidence=1.0):
    return Signal(
        timestamp=T0, direction=direction, score=80, confidence=confidence,
        reasoning="test", stop_loss=stop, target_1=t1, target_2=t2, target_3=t3,
        risk_reward=1.5,
    )


def test_one_lot_over_the_allowed_risk_is_zero_lots(monkeypatch):
    monkeypatch.setattr(settings, "CAPITAL", 100_000.0)
    monkeypatch.setattr(settings, "RISK_PER_TRADE", 0.01)
    # 150-point stop, premium 600: a unit loses at most 150.
    sized = RiskManager().size_option_position(signal(), 600.0, 51012.0, 30, 100_000.0)
    assert (sized.qty, sized.risk_per_unit, sized.allowed_risk) == (0, 150.0, 1000.0)
    assert sized.why_zero() == (
        "Position size 0: one lot of 30 risks ₹4,500 to the stop, more than the ₹1,000 allowed"
    )


def test_lots_fit_the_risk_and_a_cheap_premium_caps_the_unit_risk(monkeypatch):
    monkeypatch.setattr(settings, "CAPITAL", 1_000_000.0)
    monkeypatch.setattr(settings, "RISK_PER_TRADE", 0.01)
    rm = RiskManager()
    # ₹10,000 / 150 = 66 units -> 2 lots of 30.
    assert rm.size_option_position(signal(), 600.0, 51012.0, 30, 1e9).qty == 60
    # Premium 90 < 150-point stop: at most 90 lost per unit -> 111 -> 3 lots.
    cheap = rm.size_option_position(signal(), 90.0, 51012.0, 30, 1e9)
    assert (cheap.risk_per_unit, cheap.qty) == (90.0, 90)
    # Half confidence halves the allowed risk: 5,000 / 150 = 33 -> 1 lot.
    assert rm.size_option_position(signal(confidence=0.0), 600.0, 51012.0, 30, 1e9).qty == 30


def test_the_premium_paid_is_capped_by_the_position_limit(monkeypatch):
    monkeypatch.setattr(settings, "CAPITAL", 1_000_000.0)
    sized = RiskManager().size_option_position(signal(), 600.0, 51012.0, 30, 15_000.0)
    assert sized.qty == 0
    assert sized.why_zero() == (
        "Position size 0: one lot of 30 costs ₹18,000, more than the ₹15,000 position limit"
    )


# ── The engine ───────────────────────────────────────────────────

CALL = "NSE:BANKNIFTY26NOV51000CE"


class OptionBroker(FakeBroker):
    def __init__(self):
        super().__init__()
        self.quotes = {CALL: {"ltp": 300.0, "bid": 299.5, "ask": 300.5}}
        self.subscribed = []

    async def get_quote(self, symbol):
        return self.quotes.get(symbol)

    async def subscribe_ticks(self, symbols):
        self.subscribed.append(list(symbols))


@pytest.fixture()
async def option_bot(db_path, monkeypatch, tmp_path):
    import bot as bot_module
    from bot import NiftyBot

    class Clock(datetime):
        @classmethod
        def utcnow(cls):
            return T0 + timedelta(minutes=5)  # 10:05 IST, Monday 26 Oct 2026

    monkeypatch.setattr(bot_module, "datetime", Clock)
    monkeypatch.setattr(settings, "PAPER_TRADING", True)
    # These engine tests size on the worst case at the index stop; the
    # fixed one-lot paper mode has its own tests below.
    monkeypatch.setattr(settings, "OPTION_PAPER_ONE_LOT", False)
    monkeypatch.setattr(settings, "CAPITAL", 1_000_000.0)
    monkeypatch.setattr(settings, "RISK_PER_TRADE", 0.01)
    await init_db()

    async def fetch():
        return master_text()

    engine = NiftyBot()
    engine.broker = OptionBroker()
    engine.option_book = OptionBook(fetch, tmp_path / "NSE_FO.csv")
    engine.symbol = BANKNIFTY
    engine.symbol_name = "Bank Nifty"
    engine.is_running = True
    engine.messages = []

    async def _send(message):
        engine.messages.append(message)

    engine.notifier.send = _send
    return engine


async def test_a_long_signal_buys_the_atm_call_with_index_levels(option_bot):
    reason = await option_bot._enter_trade(signal(), 51012.0)
    assert reason is None
    trade = option_bot.active_trade
    assert trade.symbol == BANKNIFTY and trade.paper_trade
    assert (trade.option_symbol, trade.option_type, trade.option_strike, trade.option_expiry) == (
        CALL, "CE", 51000.0, NOV_24,
    )
    # Bought at the ask; 10,000 / min(300.5, 150) = 66 units -> 2 lots.
    assert (trade.entry_price, trade.quantity, trade.lot_size) == (300.5, 60, 30)
    # Levels are the index's, measured from the index close.
    assert (trade.underlying_entry_price, trade.stop_loss, trade.target_1) == (51012.0, 50862.0, 51237.0)
    # Its ticks are subscribed, and the notice names the contract.
    assert CALL in option_bot.broker.subscribed[-1]
    assert "Bought: BANKNIFTY 24 Nov 2026 51000 CE (lot 30) = NSE:BANKNIFTY26NOV51000CE" in option_bot.messages[-1]
    # Stored and restored with every option field.
    saved = await get_open_trade()
    assert (saved.option_symbol, saved.option_expiry, saved.lot_size, saved.underlying_entry_price) == (
        CALL, NOV_24, 30, 51012.0,
    )


async def test_short_signals_are_not_traded(option_bot):
    reason = await option_bot._enter_trade(
        signal(stop=51162.0, t1=50787.0, t2=50637.0, t3=50487.0, direction=Direction.SHORT), 51012.0
    )
    assert reason == "Bank Nifty is traded with call options only: SHORT signals are not traded"
    assert option_bot.active_trade is None


async def test_live_mode_refuses_options(option_bot, monkeypatch):
    monkeypatch.setattr(settings, "PAPER_TRADING", False)
    reason = await option_bot._enter_trade(signal(), 51012.0)
    assert reason.startswith("Bank Nifty options are paper-traded only")
    assert option_bot.active_trade is None and option_bot.broker.calls == []


async def test_no_option_price_means_no_trade(option_bot):
    option_bot.broker.quotes = {CALL: None}
    reason = await option_bot._enter_trade(signal(), 51012.0)
    assert reason == (
        "No price for NSE:BANKNIFTY26NOV51000CE (BANKNIFTY 24 Nov 2026 51000 CE (lot 30)): not entered"
    )
    assert option_bot.active_trade is None


async def test_too_big_a_lot_says_why_in_numbers(option_bot, monkeypatch):
    monkeypatch.setattr(settings, "CAPITAL", 100_000.0)
    reason = await option_bot._enter_trade(signal(), 51012.0)
    assert reason == (
        "BANKNIFTY 24 Nov 2026 51000 CE (lot 30) at ₹300.50: Position size 0: one lot of 30 "
        "risks ₹4,500 to the stop, more than the ₹1,000 allowed (worst case per unit: the "
        "smaller of the premium and the 150.00-point index stop)"
    )


async def test_index_stop_sells_the_call_at_its_bid(option_bot):
    await option_bot._enter_trade(signal(), 51012.0)
    trade = option_bot.active_trade
    option_bot.broker.quotes[CALL] = {"ltp": 231.0, "bid": 230.5, "ask": 231.5}
    # The index above the stop: nothing happens, whatever the premium does.
    await option_bot._manage_trade(50900.0, T0 + timedelta(minutes=20))
    assert option_bot.active_trade is trade
    # The index reaches its stop: the call is sold at its bid.
    await option_bot._manage_trade(50860.0, T0 + timedelta(minutes=30))
    assert option_bot.active_trade is None
    assert (trade.exit_reason, trade.exit_price) == ("STOP_LOSS", 230.5)
    assert trade.realized_pnl == pytest.approx((230.5 - 300.5) * 60)
    assert trade.result == SignalResult.LOSS


async def test_targets_are_index_levels_and_t1_moves_the_stop_to_the_index_entry(option_bot):
    await option_bot._enter_trade(signal(), 51012.0)
    trade = option_bot.active_trade
    option_bot.broker.quotes[CALL] = {"ltp": 420.0, "bid": 419.5, "ask": 420.5}
    await option_bot._manage_trade(51240.0, T0 + timedelta(minutes=20))
    # Half of 2 lots at T1, sold at the call's bid (not at the index target).
    assert trade.status == TradeStatus.PARTIAL_1
    assert (trade.exit_t1_qty, trade.exit_t1_price, trade.remaining_qty) == (30, 419.5, 30)
    assert trade.realized_pnl == pytest.approx((419.5 - 300.5) * 30)
    # Breakeven is the INDEX entry, not the premium.
    assert trade.stop_loss == 51012.0
    option_bot.broker.quotes[CALL] = {"ltp": 305.0, "bid": 304.5, "ask": 305.5}
    await option_bot._manage_trade(51010.0, T0 + timedelta(minutes=40))
    assert (trade.exit_reason, trade.exit_price) == ("STOP_LOSS", 304.5)
    assert trade.realized_pnl == pytest.approx((419.5 - 300.5) * 30 + (304.5 - 300.5) * 30)


async def test_one_lot_is_not_split_at_t1(option_bot, monkeypatch):
    monkeypatch.setattr(settings, "CAPITAL", 500_000.0)  # 5,000 / 150 -> 1 lot
    await option_bot._enter_trade(signal(), 51012.0)
    trade = option_bot.active_trade
    assert trade.quantity == 30
    await option_bot._manage_trade(51240.0, T0 + timedelta(minutes=20))
    assert (trade.status, trade.remaining_qty, trade.stop_loss) == (TradeStatus.PARTIAL_1, 30, 51012.0)


async def test_an_exit_without_an_option_price_is_postponed(option_bot):
    await option_bot._enter_trade(signal(), 51012.0)
    trade = option_bot.active_trade
    option_bot.broker.quotes[CALL] = None
    await option_bot._manage_trade(50860.0, T0 + timedelta(minutes=30))
    assert option_bot.active_trade is trade and trade.remaining_qty == 60
    assert "No price for NSE:BANKNIFTY26NOV51000CE: exit of" in option_bot.messages[-1]
    # A recent tick of the call is used once the quote has nothing.
    option_bot._option_marks[CALL] = (240.0, T0 + timedelta(minutes=5))
    option_bot._exit_retry_at = 0.0
    await option_bot._manage_trade(50860.0, T0 + timedelta(minutes=31))
    assert (trade.exit_reason, trade.exit_price) == ("STOP_LOSS", 240.0)


async def test_a_stale_option_tick_is_not_a_price(option_bot):
    await option_bot._enter_trade(signal(), 51012.0)
    option_bot.broker.quotes[CALL] = None
    option_bot._option_marks[CALL] = (240.0, T0 - timedelta(minutes=10))
    await option_bot._manage_trade(50860.0, T0 + timedelta(minutes=30))
    assert option_bot.active_trade is not None


async def test_option_ticks_mark_the_trade_and_stay_off_the_watchlist(option_bot):
    await option_bot._enter_trade(signal(), 51012.0)
    await option_bot._on_tick({"symbol": CALL, "ltp": 333.0, "v": 1000})
    assert CALL not in option_bot.watchlist_quotes
    position = option_bot.open_position_pnl()
    assert (position["option_symbol"], position["price"]) == (CALL, 333.0)
    assert position["unrealized_pnl"] == pytest.approx((333.0 - 300.5) * 60)
    # The index's own price never marks the call.
    option_bot._last_price = 51500.0
    assert option_bot.open_position_pnl()["price"] == 333.0


async def test_manual_exit_sells_the_call(option_bot):
    await option_bot._enter_trade(signal(), 51012.0)
    trade = option_bot.active_trade
    option_bot._last_price = None  # no index tick yet: the call has its own quote
    result = await option_bot.exit_trade_now(trade.id)
    assert (trade.exit_reason, trade.exit_price) == ("MANUAL_EXIT", 299.5)
    assert result["message"].endswith("closed at 299.5: P&L -60.00")


def test_breakeven_uses_the_index_entry_only_for_option_trades():
    from tests.test_trading_safety import make_trade

    equity = make_trade()
    assert trade_rules.level_entry(equity) == 800.0
    option = make_trade(entry_price=300.5, underlying_entry_price=51012.0, stop_loss=50862.0)
    assert trade_rules.breakeven_stop(option) == 51012.0


async def test_the_square_off_sells_the_call_without_an_index_tick(option_bot):
    await option_bot._enter_trade(signal(), 51012.0)
    trade = option_bot.active_trade
    option_bot._last_price = None
    # Left open from an earlier day (a crash): squared off at once.
    trade.entry_time = T0 - timedelta(days=1)
    await option_bot._supervise_trade()
    assert option_bot.active_trade is None
    assert (trade.exit_reason, trade.exit_price) == ("EOD_SQUARE_OFF", 299.5)


# ── Paper: always one lot (OPTION_PAPER_ONE_LOT) ─────────────────

def test_one_lot_mode_is_the_default():
    from config import Settings

    assert Settings.model_fields["OPTION_PAPER_ONE_LOT"].default is True


async def test_paper_buys_one_lot_whatever_the_risk_rule_allows(option_bot, monkeypatch):
    monkeypatch.setattr(settings, "OPTION_PAPER_ONE_LOT", True)
    monkeypatch.setattr(settings, "CAPITAL", 18_000.0)
    # 1% of 18,000 is 180; one lot can lose 150 x 30 = 4,500 at the stop.
    assert await option_bot._enter_trade(signal(), 51012.0) is None
    trade = option_bot.active_trade
    assert (trade.option_symbol, trade.quantity, trade.entry_price) == (CALL, 30, 300.5)
    assert "(1 lot, fixed paper size)" in option_bot.messages[-1]


async def test_one_lot_is_refused_only_when_it_costs_more_than_the_capital(option_bot, monkeypatch):
    monkeypatch.setattr(settings, "OPTION_PAPER_ONE_LOT", True)
    monkeypatch.setattr(settings, "CAPITAL", 18_000.0)
    option_bot.broker.quotes[CALL] = {"ltp": 649.0, "bid": 648.5, "ask": 650.0}
    reason = await option_bot._enter_trade(signal(), 51012.0)
    assert reason == (
        "BANKNIFTY 24 Nov 2026 51000 CE (lot 30) at ₹650.00: Position size 0: one lot of 30 "
        "costs ₹19,500, more than the ₹18,000 position limit"
    )
    assert option_bot.active_trade is None
    # Exactly at the limit (600 x 30 = 18,000) it buys.
    option_bot.broker.quotes[CALL] = {"ltp": 599.5, "bid": 599.0, "ask": 600.0}
    assert await option_bot._enter_trade(signal(), 51012.0) is None
    assert option_bot.active_trade.quantity == 30


async def test_one_lot_mode_never_applies_live(option_bot, monkeypatch):
    monkeypatch.setattr(settings, "OPTION_PAPER_ONE_LOT", True)
    monkeypatch.setattr(settings, "PAPER_TRADING", False)
    reason = await option_bot._enter_trade(signal(), 51012.0)
    assert reason.startswith("Bank Nifty options are paper-traded only")
    assert option_bot.broker.calls == []


async def test_the_loss_limits_still_gate_one_lot_trades(option_bot, monkeypatch):
    monkeypatch.setattr(settings, "CAPITAL", 18_000.0)
    rm = option_bot.risk_manager
    rm.day = "2026-10-26"
    rm.week = "2026-W44"
    rm.daily_pnl = -2_100.0  # one losing lot: far past the 450 daily limit
    ok, reason = await rm.can_trade(datetime(2026, 10, 26, 11, 0))
    assert not ok and reason == "Daily loss limit hit: ₹-2,100.00 (limit: ₹-450.00)"
