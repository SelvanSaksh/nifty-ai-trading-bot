"""Phase 1 trading safety: IST hours, limits that survive restarts, one
position at a time, broker exits and stop-loss, square-off, restart recovery."""
import json
from datetime import datetime, timedelta

import pytest

from brokers.base import ORDER_CANCELLED, ORDER_FILLED, ORDER_PENDING
from brokers.fyers_broker import FyersBroker
from brokers.fyers_broker_mock import FyersBroker as MockBroker
from config import settings
from database import get_open_trade, get_recent_trades, init_db, save_trade
from models.enums import Direction, SignalResult, TradeStatus
from models.signal import Signal
from models.trade import Trade
from risk_manager import RiskManager
from utils.market_time import ist_now, to_ist

# Monday 2026-10-26. 10:00 IST = 04:30 UTC; 15:15 IST = 09:45 UTC.
MONDAY_10_IST = datetime(2026, 10, 26, 10, 0)
T0 = datetime(2026, 10, 26, 4, 30)  # the same moment in UTC
SQUARE_OFF_UTC = datetime(2026, 10, 26, 9, 45)

EQ = "NSE:SBIN-EQ"


# Confidence 1.0 sizes at the full RISK_PER_TRADE (₹1,000): the trade
# mechanics below are written for that size.
def make_signal(direction=Direction.LONG, stop=790.0, t1=820.0, t2=830.0, t3=840.0, confidence=1.0):
    return Signal(
        timestamp=T0, direction=direction, score=80, confidence=confidence,
        reasoning="test", stop_loss=stop, target_1=t1, target_2=t2, target_3=t3,
        risk_reward=2.0,
    )


def make_trade(**overrides):
    fields = dict(
        entry_time=T0, direction=Direction.LONG, entry_price=800.0, quantity=100,
        initial_qty=100, remaining_qty=100, stop_loss=790.0, target_1=820.0,
        target_2=830.0, target_3=840.0, symbol=EQ, symbol_name="SBI",
        signal_score=80, confidence=0.5, paper_trade=True,
    )
    fields.update(overrides)
    return Trade(**fields)


# ── IST clock ─────────────────────────────────────────────────────

def test_ist_is_utc_plus_five_thirty():
    assert to_ist(T0) == MONDAY_10_IST
    drift = ist_now() - (datetime.utcnow() + timedelta(hours=5, minutes=30))
    assert abs(drift.total_seconds()) < 2


class TestTradingHours:
    @pytest.fixture()
    async def rm(self, db_path):
        await init_db()
        return RiskManager()

    async def test_open_during_the_ist_session(self, rm):
        assert await rm.can_trade(MONDAY_10_IST) == (True, "OK")

    @pytest.mark.parametrize("hh,mm", [(9, 14), (15, 15), (15, 29), (20, 0), (2, 0)])
    async def test_closed_outside_it(self, rm, hh, mm):
        ok, reason = await rm.can_trade(MONDAY_10_IST.replace(hour=hh, minute=mm))
        assert not ok
        assert "Outside trading hours (09:15-15:15 IST)" == reason

    async def test_the_default_clock_is_ist_not_the_server_zone(self, rm, monkeypatch):
        import risk_manager

        monkeypatch.setattr(risk_manager, "ist_now", lambda: MONDAY_10_IST)
        assert (await rm.can_trade())[0]
        # The same instant read as UTC (the Docker clock) is before the open.
        assert not (await rm.can_trade(T0))[0]


# ── Daily / weekly limits ─────────────────────────────────────────

class TestRollOver:
    def loss(self, pnl):
        return make_trade(realized_pnl=pnl, result=SignalResult.LOSS)

    def test_a_new_day_resets_the_daily_counters_only(self):
        rm = RiskManager()
        rm.update_after_trade(self.loss(-500), now=MONDAY_10_IST)
        rm.update_after_trade(self.loss(-500), now=MONDAY_10_IST)
        assert (rm.trades_today, rm.daily_pnl, rm.weekly_pnl) == (2, -1000, -1000)
        assert rm.cooldown_until is not None

        assert rm.roll_over(MONDAY_10_IST + timedelta(hours=5)) is False  # same day
        assert rm.roll_over(MONDAY_10_IST + timedelta(days=1)) is True
        assert (rm.trades_today, rm.daily_pnl, rm.consecutive_losses) == (0, 0, 0)
        assert rm.cooldown_until is None
        assert rm.weekly_pnl == -1000

    def test_a_new_week_resets_the_weekly_pnl(self):
        rm = RiskManager()
        rm.update_after_trade(self.loss(-500), now=MONDAY_10_IST)
        rm.roll_over(MONDAY_10_IST + timedelta(days=6))  # Sunday, same ISO week
        assert rm.weekly_pnl == -500
        rm.roll_over(MONDAY_10_IST + timedelta(days=7))  # next Monday
        assert rm.weekly_pnl == 0

    async def test_the_daily_loss_limit_survives_a_restart(self, db_path):
        await init_db()
        rm = RiskManager()
        rm.update_after_trade(self.loss(-3000), now=MONDAY_10_IST)
        await rm.save()

        restarted = RiskManager()
        await restarted.load(now=MONDAY_10_IST + timedelta(minutes=5))
        assert (restarted.daily_pnl, restarted.trades_today) == (-3000, 1)
        ok, reason = await restarted.can_trade(MONDAY_10_IST + timedelta(minutes=5))
        assert not ok and reason.startswith("Daily loss limit hit")

    async def test_loading_on_a_later_day_starts_fresh(self, db_path):
        await init_db()
        rm = RiskManager()
        rm.update_after_trade(self.loss(-3000), now=MONDAY_10_IST)
        await rm.save()

        restarted = RiskManager()
        await restarted.load(now=MONDAY_10_IST + timedelta(days=1))
        assert restarted.daily_pnl == 0
        assert restarted.weekly_pnl == -3000
        assert (await restarted.can_trade(MONDAY_10_IST + timedelta(days=1)))[0]


# ── Position sizing ───────────────────────────────────────────────

class TestSizing:
    def test_equities_size_in_single_shares(self):
        # ₹1,000 risk / ₹10 per share.
        assert RiskManager().calculate_position_size(make_signal(), 800.0, 1) == 100

    def test_whole_lots_only(self):
        signal = make_signal(stop=24495.0)  # 5 points of risk -> 200 units
        assert RiskManager().calculate_position_size(signal, 24500.0, 75) == 150

    @pytest.mark.parametrize("confidence, qty", [
        (0.0, 50),    # 50% of ₹1,000
        (0.55, 77),   # a score-55 signal: 77.5% -> ₹775 / ₹10
        (1.0, 100),   # the full ₹1,000, never more
        (1.7, 100),   # out-of-range confidence is clamped
        (-0.3, 50),
    ])
    def test_confidence_scales_within_the_risk_limit(self, confidence, qty):
        signal = make_signal(confidence=confidence)
        assert RiskManager().calculate_position_size(signal, 800.0, 1) == qty

    def test_risk_never_exceeds_risk_per_trade(self):
        allowed = settings.CAPITAL * settings.RISK_PER_TRADE
        for step in range(0, 101):
            signal = make_signal(confidence=step / 100)
            qty = RiskManager().calculate_position_size(signal, 800.0, 1)
            assert qty * (800.0 - signal.stop_loss) <= allowed, step / 100

    def test_no_trade_when_one_lot_risks_too_much(self):
        signal = make_signal(stop=24440.0)  # 60 points x 50 = ₹3,000 > ₹1,000
        assert RiskManager().calculate_position_size(signal, 24500.0, 50) == 0

    def test_lot_size_by_instrument(self, monkeypatch):
        monkeypatch.setattr(settings, "LOT_SIZE", 65)
        assert RiskManager.lot_size_for("NSE:SBIN-EQ") == 1
        assert RiskManager.lot_size_for("NSE:NIFTY50-INDEX") == 65


# ── Database ──────────────────────────────────────────────────────

async def test_trades_are_saved_and_read_back(db_path):
    await init_db()
    trade = make_trade(
        paper_trade=False, entry_order_id="E1", sl_order_id="S1",
        status=TradeStatus.PARTIAL_1, exit_t1_price=820.0, exit_t1_qty=50,
        exit_t1_time=T0 + timedelta(minutes=30), remaining_qty=50, realized_pnl=1000.0,
    )
    await save_trade(trade)
    assert await get_open_trade() == trade

    trade.status = TradeStatus.CLOSED
    trade.exit_reason = "TARGET_3"
    await save_trade(trade)
    assert await get_open_trade() is None
    assert (await get_recent_trades(5)) == [trade]


# ── Engine fixtures ───────────────────────────────────────────────

class FakeBroker(MockBroker):
    """Mock order book that records every order call."""

    def __init__(self):
        super().__init__()
        self.calls = []
        self.refuse_stops = False
        self.market_fills = True
        self.positions_error = None

    async def place_market_order(self, symbol, side, qty):
        self.calls.append(("market", side, qty))
        if not self.market_fills:
            order = self._new_order(symbol, side, qty)
            order["status"] = ORDER_CANCELLED
            return {"s": "ok", "id": order["id"]}
        return await super().place_market_order(symbol, side, qty)

    async def place_stop_order(self, symbol, side, qty, stop_price):
        self.calls.append(("stop", side, qty, stop_price))
        if self.refuse_stops:
            return {"s": "error", "message": "stop refused"}
        return await super().place_stop_order(symbol, side, qty, stop_price)

    async def modify_order(self, order_id, **kwargs):
        self.calls.append(("modify", order_id, kwargs.get("qty")))
        return await super().modify_order(order_id, **kwargs)

    async def cancel_order(self, order_id):
        self.calls.append(("cancel", order_id))
        return await super().cancel_order(order_id)

    async def get_positions(self):
        if self.positions_error:
            raise ConnectionError(self.positions_error)
        return await super().get_positions()

    def fire_stop(self, order_id, price):
        order = self._orders[order_id]
        order.update(status=ORDER_FILLED, filled_qty=order["qty"], price=price)
        self._net_qty[order["symbol"]] += order["side"] * order["qty"]

    def kinds(self):
        return [call[0] for call in self.calls]


def _engine(monkeypatch, symbol, paper):
    from bot import NiftyBot

    monkeypatch.setattr(settings, "PAPER_TRADING", paper)
    engine = NiftyBot()
    engine.broker = FakeBroker()
    engine.broker._symbol_prices[symbol] = 800.0
    engine.symbol = symbol
    engine.is_running = True
    engine.ORDER_FILL_POLL_SECONDS = 0
    engine.messages = []

    async def _send(message):
        engine.messages.append(message)

    engine.notifier.send = _send
    return engine


@pytest.fixture()
async def paper_bot(db_path, monkeypatch):
    await init_db()
    return _engine(monkeypatch, EQ, paper=True)


@pytest.fixture()
async def live_bot(db_path, monkeypatch):
    await init_db()
    return _engine(monkeypatch, EQ, paper=False)


async def enter(engine, signal=None, price=800.0):
    reason = await engine._enter_trade(signal or make_signal(), price)
    if engine.active_trade is not None:
        engine.active_trade.entry_time = T0
        await save_trade(engine.active_trade)
    return reason


@pytest.fixture()
def morning(monkeypatch):
    """Engine clock at 10:05 IST on the test Monday (before the square-off)."""
    import bot as bot_module

    class Clock(datetime):
        @classmethod
        def utcnow(cls):
            return T0 + timedelta(minutes=5)

    monkeypatch.setattr(bot_module, "datetime", Clock)


# ── One position at a time ────────────────────────────────────────

async def test_only_one_trade_can_be_open(paper_bot):
    assert await enter(paper_bot) is None
    first = paper_bot.active_trade
    reason = await enter(paper_bot, make_signal(direction=Direction.SHORT, stop=810.0))
    assert reason == f"Trade {first.id} is already open"
    assert paper_bot.active_trade is first
    assert len(await get_recent_trades(10)) == 1


async def test_an_oversized_trade_is_skipped(paper_bot):
    reason = await enter(paper_bot, make_signal(stop=700.0, confidence=0.0), price=800.0)
    # ₹500 risk / ₹100 per share = 5 shares is still a trade...
    assert reason is None
    paper_bot.active_trade = None
    paper_bot.symbol = "NSE:NIFTY50-INDEX"  # ...but not one 50-unit lot.
    assert (await enter(paper_bot, make_signal(stop=700.0), price=800.0)).startswith("Position size 0")


# ── Paper exits ───────────────────────────────────────────────────

async def test_paper_trades_never_reach_the_broker(paper_bot):
    await enter(paper_bot)
    await paper_bot._manage_trade(821.0, T0 + timedelta(minutes=5))
    await paper_bot._manage_trade(785.0, T0 + timedelta(minutes=10))
    assert paper_bot.broker.calls == []
    assert paper_bot.active_trade is None


async def test_a_stop_is_booked_at_the_price_that_crossed_it(paper_bot):
    await enter(paper_bot)
    trade = paper_bot.active_trade
    await paper_bot._manage_trade(787.0, T0 + timedelta(minutes=5))
    assert paper_bot.active_trade is None
    assert (trade.exit_reason, trade.exit_price, trade.exit_sl_price) == ("STOP_LOSS", 787.0, 787.0)
    assert trade.realized_pnl == -1300.0
    assert paper_bot.today_pnl == -1300.0
    saved = (await get_recent_trades(1))[0]
    assert saved.status == TradeStatus.CLOSED and saved.realized_pnl == -1300.0


async def test_partial_exits_are_whole_lots(paper_bot):
    paper_bot.symbol = "NSE:NIFTY50-INDEX"
    await enter(paper_bot, make_signal(stop=24495.0, t1=24510, t2=24520, t3=24530), price=24500.0)
    trade = paper_bot.active_trade
    assert trade.quantity == 200
    await paper_bot._manage_trade(24510.0, T0 + timedelta(minutes=5))
    assert (trade.exit_t1_qty, trade.remaining_qty) == (100, 100)
    await paper_bot._manage_trade(24520.0, T0 + timedelta(minutes=10))
    assert (trade.exit_t2_qty, trade.remaining_qty) == (50, 50)  # 75 -> one lot of 50
    await paper_bot._manage_trade(24530.0, T0 + timedelta(minutes=15))
    assert trade.exit_t3_qty == 50 and trade.status == TradeStatus.CLOSED


async def test_a_single_lot_rides_to_the_next_target(paper_bot):
    paper_bot.symbol = "NSE:NIFTY50-INDEX"
    await enter(paper_bot, make_signal(stop=24480.0, t1=24510, t2=24520, t3=24530), price=24500.0)
    trade = paper_bot.active_trade
    assert trade.quantity == 50
    await paper_bot._manage_trade(24510.0, T0 + timedelta(minutes=5))
    assert trade.status == TradeStatus.PARTIAL_1
    assert (trade.exit_t1_qty, trade.remaining_qty) == (None, 50)


# ── End-of-day square-off ─────────────────────────────────────────

async def test_square_off_at_15_15_ist(paper_bot):
    await enter(paper_bot)
    trade = paper_bot.active_trade
    await paper_bot._manage_trade(805.0, SQUARE_OFF_UTC - timedelta(seconds=1))
    assert paper_bot.active_trade is trade
    await paper_bot._manage_trade(805.0, SQUARE_OFF_UTC)
    assert paper_bot.active_trade is None
    assert (trade.exit_reason, trade.exit_price, trade.realized_pnl) == ("EOD_SQUARE_OFF", 805.0, 500.0)


async def test_square_off_also_runs_without_ticks(paper_bot, monkeypatch):
    import bot as bot_module

    class Clock(datetime):
        @classmethod
        def utcnow(cls):
            return SQUARE_OFF_UTC + timedelta(minutes=1)

    monkeypatch.setattr(bot_module, "datetime", Clock)
    await enter(paper_bot)
    trade = paper_bot.active_trade
    paper_bot._last_price = 802.0
    await paper_bot._supervise_trade()
    assert paper_bot.active_trade is None
    assert (trade.exit_reason, trade.exit_price) == ("EOD_SQUARE_OFF", 802.0)


async def test_a_trade_from_an_earlier_day_is_squared_off(paper_bot):
    await enter(paper_bot)
    paper_bot.active_trade.entry_time = T0 - timedelta(days=1)
    await paper_bot._manage_trade(801.0, T0)
    assert paper_bot.active_trade is None


# ── Live orders ───────────────────────────────────────────────────

async def test_live_entry_places_a_broker_stop(live_bot):
    assert await enter(live_bot) is None
    trade = live_bot.active_trade
    assert live_bot.broker.calls == [("market", 1, 100), ("stop", -1, 100, 790.0)]
    assert trade.entry_order_id and trade.sl_order_id
    assert (await get_open_trade()).sl_order_id == trade.sl_order_id


async def test_live_entry_uses_the_broker_fill_price(live_bot):
    live_bot.broker._symbol_prices[EQ] = 801.5
    await enter(live_bot)
    assert live_bot.active_trade.entry_price == 801.5


async def test_a_short_protects_with_a_buy_stop(live_bot):
    await enter(live_bot, make_signal(direction=Direction.SHORT, stop=810.0, t1=780, t2=770, t3=760))
    assert live_bot.broker.calls == [("market", -1, 100), ("stop", 1, 100, 810.0)]


async def test_no_stop_means_no_position(live_bot):
    live_bot.broker.refuse_stops = True
    assert await enter(live_bot) is None
    assert live_bot.broker.kinds() == ["market", "stop", "market"]
    assert live_bot.broker.calls[-1] == ("market", -1, 100)
    assert live_bot.active_trade is None
    assert (await get_recent_trades(1))[0].exit_reason == "STOP_ORDER_FAILED"


async def test_an_index_is_never_traded_live(live_bot):
    live_bot.symbol = "NSE:NIFTY50-INDEX"
    reason = await enter(live_bot)
    assert "cannot be traded live" in reason
    assert live_bot.broker.calls == []


async def test_an_unfilled_entry_opens_nothing(live_bot):
    live_bot.broker.market_fills = False
    assert await enter(live_bot) == "Entry order did not fill (see alerts)"
    assert live_bot.active_trade is None
    assert live_bot.broker.kinds() == ["market"]


async def test_an_entry_in_unknown_state_blocks_trading(live_bot):
    async def _unreadable(order_id):
        return None

    live_bot.broker.get_order = _unreadable
    await enter(live_bot)
    assert live_bot.active_trade is None
    assert live_bot._trade_frozen and "unknown state" in live_bot.trading_halted
    assert (await enter(live_bot)).startswith("New trades blocked")


async def test_a_partial_exit_shrinks_the_stop_before_selling(live_bot):
    await enter(live_bot)
    trade = live_bot.active_trade
    live_bot.broker.calls.clear()
    await live_bot._manage_trade(820.0, T0 + timedelta(minutes=5))
    assert live_bot.broker.calls == [("modify", trade.sl_order_id, 50), ("market", -1, 50)]
    assert (trade.remaining_qty, trade.status) == (50, TradeStatus.PARTIAL_1)
    assert live_bot.broker._orders[trade.sl_order_id]["qty"] == 50


async def test_a_full_exit_cancels_the_stop_before_selling(live_bot):
    await enter(live_bot)
    stop_id = live_bot.active_trade.sl_order_id
    live_bot.broker.calls.clear()
    await live_bot._manage_trade(780.0, T0 + timedelta(minutes=5))
    assert live_bot.broker.calls == [("cancel", stop_id), ("market", -1, 100)]
    assert live_bot.active_trade is None
    assert live_bot.broker._net_qty[EQ] == 0


async def test_a_fired_broker_stop_is_never_sold_twice(live_bot):
    await enter(live_bot)
    trade = live_bot.active_trade
    live_bot.broker.fire_stop(trade.sl_order_id, 789.5)
    live_bot.broker.calls.clear()
    await live_bot._manage_trade(789.0, T0 + timedelta(minutes=5))
    assert live_bot.broker.calls == []
    assert (trade.exit_reason, trade.exit_price) == ("STOP_LOSS", 789.5)
    assert live_bot.broker._net_qty[EQ] == 0


async def test_a_fired_stop_is_noticed_without_ticks(live_bot, morning):
    await enter(live_bot)
    trade = live_bot.active_trade
    live_bot.broker.fire_stop(trade.sl_order_id, 789.0)
    await live_bot._supervise_trade()
    assert live_bot.active_trade is None and trade.exit_reason == "STOP_LOSS"


async def test_an_exit_that_does_not_fill_keeps_the_position_protected(live_bot):
    await enter(live_bot)
    trade = live_bot.active_trade
    live_bot.broker.market_fills = False
    live_bot.broker.calls.clear()
    await live_bot._manage_trade(780.0, T0 + timedelta(minutes=5))
    assert live_bot.active_trade is trade and trade.remaining_qty == 100
    # Stop cancelled, sell failed, a new stop for the full position.
    assert live_bot.broker.calls == [
        ("cancel", live_bot.broker.calls[0][1]),
        ("market", -1, 100),
        ("stop", -1, 100, 790.0),
    ]
    assert trade.sl_order_id is not None
    # Not retried on the very next tick.
    live_bot.broker.calls.clear()
    await live_bot._manage_trade(779.0, T0 + timedelta(minutes=5, seconds=1))
    assert live_bot.broker.calls == []


async def test_a_cancelled_broker_stop_is_replaced(live_bot, morning):
    await enter(live_bot)
    trade = live_bot.active_trade
    old = trade.sl_order_id
    await live_bot.broker.cancel_order(old)  # e.g. cancelled by hand in the Fyers app
    live_bot.broker.calls.clear()
    await live_bot._supervise_trade()
    assert live_bot.broker.calls == [("stop", -1, 100, 790.0)]
    assert trade.sl_order_id not in (None, old)


async def test_stopping_the_bot_exits_through_the_broker(live_bot):
    await enter(live_bot)
    live_bot._last_price = 803.0
    live_bot.broker._symbol_prices[EQ] = 803.0
    trade = live_bot.active_trade
    await live_bot._emergency_exit("Bot stopped")
    assert live_bot.broker.kinds()[-2:] == ["cancel", "market"]
    assert trade.exit_reason == "EMERGENCY: Bot stopped"
    assert trade.exit_price == 803.0  # not the entry price


# ── Restart recovery ──────────────────────────────────────────────

async def test_an_open_paper_trade_is_restored(paper_bot):
    await enter(paper_bot)
    trade = paper_bot.active_trade
    paper_bot.active_trade = None  # a crash
    await paper_bot._restore_open_trade()
    assert paper_bot.active_trade == trade
    assert paper_bot.trading_halted is None


async def test_a_live_trade_is_restored_when_the_broker_agrees(live_bot):
    await enter(live_bot)
    trade = live_bot.active_trade
    live_bot.active_trade = None
    await live_bot._restore_open_trade()
    assert live_bot.active_trade == trade
    assert live_bot.trading_halted is None


async def test_a_trade_the_broker_stopped_out_is_booked_on_restart(live_bot):
    await enter(live_bot)
    trade = live_bot.active_trade
    live_bot.broker.fire_stop(trade.sl_order_id, 789.0)
    live_bot.active_trade = None
    await live_bot._restore_open_trade()
    assert live_bot.active_trade is None
    saved = (await get_recent_trades(1))[0]
    assert (saved.exit_reason, saved.exit_price, saved.realized_pnl) == ("STOP_LOSS", 789.0, -1100.0)
    assert live_bot.risk_manager.daily_pnl == -1100.0


async def test_a_position_closed_elsewhere_cancels_its_stop(live_bot):
    await enter(live_bot)
    trade = live_bot.active_trade
    await live_bot.broker.place_market_order(EQ, -1, 100)  # squared off by the broker
    live_bot.active_trade = None
    await live_bot._restore_open_trade()
    assert live_bot.broker._orders[trade.sl_order_id]["status"] == ORDER_CANCELLED
    saved = (await get_recent_trades(1))[0]
    assert (saved.status, saved.exit_reason, saved.result) == (TradeStatus.CLOSED, "CLOSED_OUTSIDE_BOT", None)
    assert live_bot.trading_halted is None


async def test_a_mismatched_position_blocks_new_trades(live_bot):
    await enter(live_bot)
    await live_bot.broker.place_market_order(EQ, 1, 10)  # bought by hand
    live_bot.active_trade = None
    await live_bot._restore_open_trade()
    assert live_bot.active_trade is None
    assert "does not match" in live_bot.trading_halted
    assert (await enter(live_bot)).startswith("New trades blocked")


async def test_an_unknown_position_blocks_new_trades(live_bot):
    await live_bot.broker.place_market_order(EQ, 1, 10)
    await live_bot._restore_open_trade()
    assert "no record of" in live_bot.trading_halted


async def test_unreadable_positions_block_new_trades(live_bot):
    live_bot.broker.positions_error = "timeout"
    await live_bot._restore_open_trade()
    assert "could not read broker positions" in live_bot.trading_halted


# ── Broadcasts ────────────────────────────────────────────────────

async def test_trade_broadcasts_reach_clients(paper_bot):
    class Client:
        def __init__(self):
            self.sent = []

        async def send_json(self, data):
            self.sent.append(json.dumps(data))  # what Starlette does

    client = Client()
    paper_bot.add_websocket_client(client)
    await enter(paper_bot)
    assert client in paper_bot.ws_clients
    message = json.loads(client.sent[-1])
    assert message["type"] == "trade_entered"
    assert message["trade"]["direction"] == "LONG"


# ── Fyers wire format ─────────────────────────────────────────────

class FakeResponse:
    def __init__(self, status, body):
        self.status, self._body = status, body

    async def text(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        status, body = self.replies.pop(0)
        return FakeResponse(status, body if isinstance(body, str) else json.dumps(body))


def fyers_with(*replies):
    broker = FyersBroker()
    session = FakeSession(*replies)

    async def _session():
        return session

    broker._ensure_session = _session
    return broker, session


async def test_fyers_orders_go_to_the_sync_endpoint():
    broker, session = fyers_with((200, {"s": "ok", "id": "26102600001"}))
    reply = await broker.place_stop_order(EQ, -1, 100, 790.0300000001)
    assert reply["id"] == "26102600001"
    method, url, kwargs = session.requests[0]
    assert (method, url) == ("POST", f"{settings.FYERS_API_URL}/orders/sync")
    assert kwargs["headers"] == {"version": "3"}
    body = kwargs["json"]
    assert (body["type"], body["side"], body["qty"], body["stopPrice"]) == (3, -1, 100, 790.05)
    assert body["productType"] == "INTRADAY"


async def test_fyers_modify_and_cancel():
    broker, session = fyers_with((200, {"s": "ok"}), (200, {"s": "error", "message": "x"}))
    assert await broker.modify_order("1", qty=50, type=3, stopPrice=790.0)
    assert not await broker.cancel_order("1")
    assert [(m, u.rsplit("/", 2)[-2:]) for m, u, _ in session.requests] == [
        ("PATCH", ["orders", "sync"]),
        ("DELETE", ["orders", "sync"]),
    ]
    assert session.requests[1][2]["json"] == {"id": "1"}


@pytest.mark.parametrize("code,state", [(2, ORDER_FILLED), (1, ORDER_CANCELLED), (6, ORDER_PENDING), (5, "REJECTED")])
async def test_fyers_order_status(code, state):
    broker, session = fyers_with((200, {"s": "ok", "orderBook": [
        {"id": "9", "status": code, "filledQty": 100 if code == 2 else 0, "tradedPrice": 801.25 if code == 2 else 0, "message": ""}
    ]}))
    order = await broker.get_order("9")
    assert order["status"] == state
    assert session.requests[0][:2] == ("GET", f"{settings.FYERS_API_URL}/orders")
    assert session.requests[0][2]["params"] == {"id": "9"}
    if code == 2:
        assert (order["filled_qty"], order["price"]) == (100, 801.25)


async def test_fyers_positions_fail_loudly():
    broker, _ = fyers_with((200, {"s": "ok", "netPositions": [
        {"symbol": EQ, "netQty": -25, "productType": "INTRADAY"}
    ]}), (503, "Service Unavailable"))
    assert await broker.get_positions() == [{"symbol": EQ, "net_qty": -25, "product": "INTRADAY"}]
    with pytest.raises(ConnectionError):
        await broker.get_positions()  # never "no positions"


async def test_fyers_paper_entries_are_not_sent(monkeypatch):
    monkeypatch.setattr(settings, "PAPER_TRADING", True)
    broker, session = fyers_with()
    reply = await broker.place_order(make_trade())
    assert reply["s"] == "ok" and session.requests == []
