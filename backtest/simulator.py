"""
Trading pass: the live decision and trade rules over a replay.

At every 15m close: RiskManager.can_trade -> DecisionEngine.evaluate ->
RiskManager.validate_signal -> sizing, as in NiftyBot._on_candle_close /
_enter_trade. Open trades are managed tick by tick with trade_rules (the
same functions the engine calls). Fills are paper fills plus slippage, and
every order pays charges.

The live engine enters at the closed candle's price; here the entry fills
at the tick that closed the candle (the next bar's first price), which is
what a real market order would get.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, FrozenSet, List, Optional, Tuple

import trade_rules
from backtest.costs import CostModel, Slippage, preset_for
from backtest.replay import DecisionPoint, Replay, Tick
from config import settings
from decision_engine import DecisionEngine
from models.enums import Direction, SignalResult, TradeStatus
from models.trade import Trade
from risk_manager import RiskManager
from utils.helpers import round_to_tick
from utils.market_time import to_ist

#: Scoring ingredients that can be switched off one at a time (ablation).
COMPONENTS = ("trend", "vwap", "rsi", "macd", "volume", "patterns", "advanced", "ht_filter")


class BacktestRiskManager(RiskManager):
    """The live risk rules without the network (news) or the database."""

    async def _get_news_status(self):
        return False, []

    async def save(self):
        pass


@dataclass(frozen=True)
class Variant:
    name: str = "baseline"
    disabled: FrozenSet[str] = frozenset()

    @classmethod
    def without(cls, component: str) -> "Variant":
        if component not in COMPONENTS:
            raise ValueError(f"unknown component {component!r}; one of {', '.join(COMPONENTS)}")
        return cls(f"no_{component}", frozenset({component}))


@dataclass
class Settings:
    symbol: str = "NSE:NIFTY50-INDEX"
    capital: float = settings.CAPITAL
    lot_size: Optional[int] = None          # None: RiskManager.lot_size_for(symbol)
    costs: Optional[CostModel] = None       # None: preset_for(symbol)
    slippage: Slippage = field(default_factory=Slippage)
    trade_from: Optional[datetime] = None   # no entries before (warm-up), naive UTC


@dataclass
class TradeRecord:
    trade: Trade
    risk_rupees: float                      # entry-to-stop risk of the full size
    charges: float = 0.0
    exit_reason: str = ""

    @property
    def gross(self) -> float:
        return self.trade.realized_pnl

    @property
    def net(self) -> float:
        return self.gross - self.charges

    @property
    def r_multiple(self) -> float:
        return self.net / self.risk_rupees if self.risk_rupees else 0.0


@dataclass
class Result:
    variant: Variant
    trades: List[TradeRecord]
    signals: int = 0                        # LONG/SHORT signals the engine produced
    skipped: Dict[str, int] = field(default_factory=dict)


def _apply_variant(point: DecisionPoint, variant: Variant) -> Tuple[dict, int]:
    """The analysis as the variant sees it, and the score threshold shift."""
    if not variant.disabled:
        return point.analysis, 0
    analysis = dict(point.analysis)
    candle = analysis["candle"].model_copy()
    off = variant.disabled
    if "trend" in off:
        candle.ema_20 = candle.ema_50 = candle.ema_200 = None
    if "vwap" in off:
        candle.vwap = None
    if "rsi" in off:
        candle.rsi = None
    if "macd" in off:
        candle.macd_hist = None
    if "patterns" in off:
        analysis["patterns"] = []
    if "advanced" in off:
        analysis["divergence"] = analysis["fvg"] = analysis["liquidity_sweep"] = None
    if "ht_filter" in off:
        analysis["higher_timeframe_trend"] = Direction.WAIT
    analysis["candle"] = candle
    # The volume score is a constant +5 on both sides: dropping it is the
    # same as needing 5 more points.
    return analysis, (5 if "volume" in off else 0)


class _Sim:
    def __init__(self, replay: Replay, variant: Variant, config: Settings):
        self.replay = replay
        self.variant = variant
        self.config = config
        self.risk = BacktestRiskManager()
        self.lot = config.lot_size or self.risk.lot_size_for(config.symbol)
        self.costs = config.costs or preset_for(config.symbol)
        self.active: Optional[TradeRecord] = None
        self.result = Result(variant, [])

    def _skip(self, reason: str):
        self.result.skipped[reason] = self.result.skipped.get(reason, 0) + 1

    def _fill(self, record: TradeRecord, side: int, price: float, qty: int) -> float:
        fill = self.config.slippage.fill(price, side)
        record.charges += self.costs.order_cost(side, fill, qty)
        return fill

    @staticmethod
    def _side(trade: Trade) -> int:
        return 1 if trade.direction == Direction.LONG else -1

    @staticmethod
    def _pnl(trade: Trade, price: float, qty: int) -> float:
        if trade.direction == Direction.LONG:
            return (price - trade.entry_price) * qty
        return (trade.entry_price - price) * qty

    # Same order as NiftyBot._on_tick: manage the open trade, then decide.
    async def run(self) -> Result:
        decisions = {p.tick_index: p for p in self.replay.decisions}
        ticks = self.replay.ticks
        # Sizing and the loss limits read settings.CAPITAL.
        original_capital = settings.CAPITAL
        settings.CAPITAL = self.config.capital
        try:
            for i, tick in enumerate(ticks):
                if self.active is not None:
                    self._manage(tick)
                point = decisions.get(i)
                if point is not None:
                    await self._decide(point, tick)
            if self.active is not None and ticks:
                self._close(ticks[-1], ticks[-1].price, "END_OF_DATA")
        finally:
            settings.CAPITAL = original_capital
        return self.result

    async def _decide(self, point: DecisionPoint, tick: Tick):
        if self.config.trade_from and tick.time < self.config.trade_from:
            return
        can_trade, reason = await self.risk.can_trade(to_ist(tick.time))
        if not can_trade:
            self._skip(reason.split(":")[0].split(" (")[0])
            return
        analysis, threshold_shift = _apply_variant(point, self.variant)
        engine = DecisionEngine()
        engine.min_threshold += threshold_shift
        signal = engine.evaluate(analysis)
        if signal.direction != Direction.WAIT:
            self.result.signals += 1
        valid, _ = self.risk.validate_signal(signal)
        if not valid or signal.direction == Direction.WAIT:
            return
        if self.active is not None:
            self._skip("Trade already open")
            return

        # NiftyBot._enter_trade: sized at the closed candle's price.
        qty = self.risk.calculate_position_size(signal, point.candle.close, self.lot)
        if qty <= 0:
            self._skip("Position size 0")
            return

        trade = Trade(
            entry_time=tick.time, direction=signal.direction, entry_price=0.0,
            quantity=qty, initial_qty=qty, remaining_qty=qty,
            stop_loss=round_to_tick(signal.stop_loss), target_1=signal.target_1,
            target_2=signal.target_2, target_3=signal.target_3,
            symbol=self.config.symbol, signal_score=signal.score,
            confidence=signal.confidence, paper_trade=True,
        )
        record = TradeRecord(trade, risk_rupees=0.0)
        trade.entry_price = self._fill(record, self._side(trade), tick.price, qty)
        record.risk_rupees = abs(trade.entry_price - trade.stop_loss) * qty
        self.active = record

    def _manage(self, tick: Tick):
        record = self.active
        trade = record.trade
        action = trade_rules.next_exit(trade, tick.price, tick.time)
        if action is None:
            return
        if action.fraction is None:
            self._close(tick, action.price, action.reason)
            return
        qty = trade_rules.partial_qty(trade, action.fraction, self.lot)
        if qty > 0:
            fill = self._fill(record, -self._side(trade), action.price, qty)
            trade.realized_pnl += self._pnl(trade, fill, qty)
            trade.remaining_qty -= qty
            if action.new_status == TradeStatus.PARTIAL_1:
                trade.exit_t1_price, trade.exit_t1_qty, trade.exit_t1_time = fill, qty, tick.time
            else:
                trade.exit_t2_price, trade.exit_t2_qty, trade.exit_t2_time = fill, qty, tick.time
        trade.status = action.new_status

    def _close(self, tick: Tick, price: float, reason: str):
        record = self.active
        trade = record.trade
        qty = trade.remaining_qty
        fill = self._fill(record, -self._side(trade), price, qty)
        trade.realized_pnl += self._pnl(trade, fill, qty)
        trade.remaining_qty = 0
        trade.status = TradeStatus.CLOSED
        trade.exit_price, trade.exit_time, trade.exit_reason = fill, tick.time, reason
        trade.result = SignalResult.WIN if trade.realized_pnl > 0 else SignalResult.LOSS
        record.exit_reason = reason
        # The live risk manager counts gross P&L (it never sees charges).
        self.risk.update_after_trade(trade, now=to_ist(tick.time))
        self.result.trades.append(record)
        self.active = None


async def simulate(replay: Replay, variant: Variant = Variant(), config: Optional[Settings] = None) -> Result:
    return await _Sim(replay, variant, config or Settings()).run()
