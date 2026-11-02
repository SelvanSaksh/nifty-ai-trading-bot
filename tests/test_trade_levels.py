"""Stop and targets from the close in ATR multiples, the equity position-value
cap, sizing and R:R rejections that state their numbers, and today's decision
summary."""
import asyncio
from datetime import datetime, timedelta

from config import public_config, settings
from database import init_db, save_decision
from decision_engine import DecisionEngine
from models.candle import Candle
from models.enums import Direction
from models.signal import Signal
from risk_manager import RiskManager
from utils.market_time import ist_now


def analysis(close=1400.0, atr=5.0, rsi=55, trend="up"):
    """Scores LONG 60 (or SHORT 60 with ``trend="down"`` and RSI 45)."""
    up = 1 if trend == "up" else -1
    candle = Candle(
        timestamp=datetime(2026, 10, 26, 4, 30), open=close, high=close + 1,
        low=close - 1, close=close, volume=0, ema_20=close - up, ema_50=close - 2 * up,
        ema_200=close - 10 * up, vwap=close - 5 * up, rsi=rsi, macd_hist=float(up), atr=atr,
    )
    return {"candle": candle, "patterns": [], "divergence": None, "fvg": None,
            "liquidity_sweep": None, "higher_timeframe_trend": Direction.WAIT}


def test_reliance_is_the_default_instrument():
    assert (settings.TRADING_SYMBOL, settings.TRADING_SYMBOL_NAME) == ("NSE:RELIANCE-EQ", "Reliance")
    config = public_config()
    assert config["trading_symbol"] == "NSE:RELIANCE-EQ"
    assert (config["stop_atr"], config["target_atr"]) == (1.0, [1.5, 2.5, 3.5])
    assert config["max_position_leverage"] == 1.0


def test_long_levels_are_atr_multiples_of_the_close():
    s = DecisionEngine().evaluate(analysis(close=1400.0, atr=5.0))
    assert s.direction == Direction.LONG
    assert (s.stop_loss, s.target_1, s.target_2, s.target_3) == (1395.0, 1407.5, 1412.5, 1417.5)
    assert s.risk_reward == 1.5


def test_short_levels_mirror_them():
    s = DecisionEngine().evaluate(analysis(close=1400.0, atr=5.0, rsi=45, trend="down"))
    assert s.direction == Direction.SHORT
    assert (s.stop_loss, s.target_1, s.target_2, s.target_3) == (1405.0, 1392.5, 1387.5, 1382.5)


def test_the_candle_shape_no_longer_fails_the_reward_risk_check():
    # A close far above the low used to put the stop under the low, so R:R
    # fell below 1.5 and the strongest candles were rejected.
    a = analysis(close=1400.0, atr=5.0)
    a["candle"] = a["candle"].model_copy(update={"low": 1392.0, "open": 1393.0})
    s = DecisionEngine().evaluate(a)
    assert s.risk_reward == 1.5
    assert RiskManager().validate_signal(s) == (True, "Signal valid")


def test_no_atr_means_no_trade_instead_of_a_made_up_one():
    a = analysis()
    a["candle"] = a["candle"].model_copy(update={"atr": None})
    s = DecisionEngine().evaluate(a)
    assert s.direction == Direction.WAIT
    assert s.reasoning == "LONG 60, but no ATR yet: stop and targets cannot be set"


def test_wait_reasons_state_the_scores():
    engine = DecisionEngine()
    engine.min_threshold = 70
    assert engine.evaluate(analysis()).reasoning == "Score below threshold: long 60, short 8, needs 70"
    engine.min_threshold = 0
    flat = analysis()
    flat["candle"] = flat["candle"].model_copy(
        update={"ema_20": None, "vwap": None, "rsi": None, "macd_hist": None}
    )
    assert engine.evaluate(flat).reasoning == "Long and short tie at 0: no clear direction"


def signal(stop, price=1400.0, t1=None, confidence=1.0, rr=1.5):
    return Signal(
        timestamp=datetime(2026, 10, 26, 4, 45), direction=Direction.LONG, score=80,
        confidence=confidence, reasoning="t", stop_loss=stop,
        target_1=t1 if t1 is not None else price + 1.5 * (price - stop),
        target_2=price + 100, target_3=price + 200, risk_reward=rr, price=price,
    )


def test_equity_size_is_capped_by_the_capital():
    risk = RiskManager()
    cap = risk.max_position_value("NSE:RELIANCE-EQ")
    assert cap == settings.CAPITAL == 100_000
    assert risk.max_position_value("NSE:NIFTY50-INDEX") is None
    # ₹1,000 / ₹5 = 200 shares by risk, but ₹1 lakh buys only 71 at 1,400.
    assert risk.size_position(signal(stop=1395.0), 1400.0, 1, cap).qty == 71
    assert risk.size_position(signal(stop=1395.0), 1400.0, 1).qty == 200
    assert risk.calculate_position_size(signal(stop=1395.0), 1400.0, 1) == 200


def test_a_zero_size_says_why_in_numbers():
    risk = RiskManager()
    by_risk = risk.size_position(signal(stop=23900.0, price=24000.0), 24000.0, 50)
    assert by_risk.qty == 0
    assert by_risk.why_zero() == (
        "Position size 0: one lot of 50 risks ₹5,000 to the stop, more than the ₹1,000 allowed"
    )
    by_value = risk.size_position(signal(stop=149_999.0, price=150_000.0), 150_000.0, 1, 100_000)
    assert by_value.qty == 0
    assert by_value.why_zero() == (
        "Position size 0: one share costs ₹150,000, more than the ₹100,000 position limit"
    )
    flat = risk.size_position(signal(stop=1400.0), 1400.0, 1)
    assert flat.why_zero() == "Position size 0: the stop is at the entry price"


def test_a_reward_risk_rejection_shows_the_distances():
    ok, reason = RiskManager().validate_signal(signal(stop=1390.0, t1=1410.0, rr=1.0))
    assert not ok
    assert reason == (
        f"R:R 1.00 below minimum {settings.MIN_RISK_REWARD}: stop 10.00 away, target 1 10.00 away"
    )


async def test_the_bot_sizes_reliance_and_explains_a_zero_size(db_path):
    from bot import NiftyBot

    await init_db()
    bot = NiftyBot()
    assert bot.symbol == "NSE:RELIANCE-EQ"
    # ₹1,000 x 0.5 (confidence 0) = ₹500 allowed; ₹100 a share: 5 shares.
    assert await bot._enter_trade(signal(stop=1300.0, confidence=0.0), 1400.0) is None
    assert bot.active_trade.quantity == 5
    bot.active_trade = None
    reason = await bot._enter_trade(signal(stop=600.0, confidence=0.0), 1400.0)
    assert reason == "Position size 0: one share risks ₹800 to the stop, more than the ₹500 allowed"


def ist_day_start_utc(day) -> datetime:
    return datetime.combine(day, datetime.min.time()) - timedelta(hours=5, minutes=30)


def decision(minute, outcome, long_score, short_score, symbol="NSE:RELIANCE-EQ", day=None):
    """A decision on a 15m candle starting 09:15 IST + ``minute``."""
    start = ist_day_start_utc(day or ist_now().date())
    return {
        "candle_time": (start + timedelta(hours=9, minutes=15 + minute)).isoformat(),
        "recorded_at": datetime.utcnow().isoformat(), "symbol": symbol, "timeframe": "15m",
        "outcome": outcome, "reason": "r", "direction": None, "score": None,
        "long_score": long_score, "short_score": short_score, "price": 1400.0,
    }


def test_the_summary_counts_today_and_names_the_nearest_misses(client):
    for d in (
        decision(0, "WAIT", 48, 8),
        decision(15, "WAIT", 52, 20),
        decision(30, "BLOCKED", 52, 31),     # a tie on long: the earlier candle wins
        decision(45, "BLOCKED", None, None),  # not scored
        decision(60, "WAIT", 90, 90, symbol="NSE:SBIN-EQ"),
        decision(0, "WAIT", 99, 99, day=ist_now().date() - timedelta(days=1)),
    ):
        asyncio.run(save_decision(d))

    body = client.get("/api/decisions/summary").json()
    assert body["day"] == ist_now().date().isoformat()
    assert (body["symbol"], body["threshold"]) == ("NSE:RELIANCE-EQ", settings.MIN_SCORE_THRESHOLD)
    assert body["checks"] == 4
    assert body["outcomes"] == {
        "BLOCKED": 2, "WAIT": 2, "REJECTED": 0, "NOT_ENTERED": 0, "ENTERED": 0,
    }
    assert body["best_long"] == {
        "score": 52, "candle_time": decision(15, "WAIT", 0, 0)["candle_time"],
        "timeframe": "15m", "outcome": "WAIT",
    }
    assert (body["best_short"]["score"], body["best_short"]["outcome"]) == (31, "BLOCKED")


def test_an_empty_day(client):
    body = client.get("/api/decisions/summary").json()
    assert body["checks"] == 0
    assert body["best_long"] is None and body["best_short"] is None
