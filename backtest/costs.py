"""
Trading costs and slippage.

Indian intraday charges per order. The rates are the published ones as far
as this code knows them; exchanges and the budget change them, so check the
current schedule (and your broker's) before trusting a result to the rupee.
Every rate can be overridden.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    name: str
    brokerage_per_order: float      # flat ₹ per executed order
    brokerage_pct_cap: float        # brokerage is min(flat, pct x turnover); 0 = flat only
    stt_sell_pct: float             # securities transaction tax, sell side
    exchange_txn_pct: float         # exchange transaction charge, both sides
    sebi_pct: float                 # SEBI turnover fee, both sides
    stamp_buy_pct: float            # stamp duty, buy side
    gst_pct: float = 18.0           # GST on brokerage + exchange + SEBI fees

    def order_cost(self, side: int, price: float, qty: int) -> float:
        """₹ charged for one order of ``qty`` at ``price`` (side 1 buy, -1 sell)."""
        if qty <= 0:
            return 0.0
        turnover = price * qty
        brokerage = self.brokerage_per_order
        if self.brokerage_pct_cap:
            brokerage = min(brokerage, turnover * self.brokerage_pct_cap / 100)
        exchange = turnover * self.exchange_txn_pct / 100
        sebi = turnover * self.sebi_pct / 100
        stt = turnover * self.stt_sell_pct / 100 if side < 0 else 0.0
        stamp = turnover * self.stamp_buy_pct / 100 if side > 0 else 0.0
        gst = (brokerage + exchange + sebi) * self.gst_pct / 100
        return brokerage + exchange + sebi + stt + stamp + gst


#: Index futures, intraday (the realistic way to trade the Nifty).
FUTURES = CostModel(
    name="futures",
    brokerage_per_order=20.0,
    brokerage_pct_cap=0.0,
    stt_sell_pct=0.02,
    exchange_txn_pct=0.00173,
    sebi_pct=0.0001,
    stamp_buy_pct=0.002,
)

#: Cash equities, intraday (MIS).
EQUITY_INTRADAY = CostModel(
    name="equity",
    brokerage_per_order=20.0,
    brokerage_pct_cap=0.03,
    stt_sell_pct=0.025,
    exchange_txn_pct=0.00297,
    sebi_pct=0.0001,
    stamp_buy_pct=0.003,
)

#: No charges at all (to see the raw edge).
NONE = CostModel("none", 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

PRESETS = {m.name: m for m in (FUTURES, EQUITY_INTRADAY, NONE)}


def preset_for(symbol: str) -> CostModel:
    """Equities pay equity charges; everything else is costed as futures
    (an index cannot be traded, so its backtest stands in for futures)."""
    return EQUITY_INTRADAY if symbol.endswith("-EQ") else FUTURES


@dataclass(frozen=True)
class Slippage:
    """Adverse fill: buys fill ``pct`` % above the price, sells below."""
    pct: float = 0.01

    def fill(self, price: float, side: int) -> float:
        return round(price * (1 + side * self.pct / 100), 2)

