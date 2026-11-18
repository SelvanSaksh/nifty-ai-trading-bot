"""Day-by-day P&L of closed trades (GET /api/pnl/daily).

A trade counts on the IST day it closed, which is also the day the risk
manager books it to (``update_after_trade`` runs at the close). Its P&L is
``realized_pnl``: every part of the position, T1/T2 included.
"""
from typing import Any, Dict, List

from models.enums import SignalResult
from models.trade import Trade
from utils.market_time import to_ist


def daily_pnl(trades: List[Trade], days: int) -> Dict[str, Any]:
    """``trades``: closed trades in any order. ``days``: how many of the most
    recent trading days (days with a close) to return.

    ``cumulative`` and the drawdown run over *every* closed trade, so the
    equity curve of the returned window starts where the earlier days left it.
    """
    ordered = sorted(trades, key=lambda t: (t.exit_time or t.entry_time, t.id))
    rows: Dict[str, Dict[str, Any]] = {}
    for trade in ordered:
        day = to_ist(trade.exit_time or trade.entry_time).date().isoformat()
        row = rows.setdefault(
            day, {"day": day, "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0}
        )
        row["trades"] += 1
        row["pnl"] += trade.realized_pnl or 0.0
        if trade.result == SignalResult.WIN:
            row["wins"] += 1
        elif trade.result == SignalResult.LOSS:
            row["losses"] += 1
        # No result: closed outside the bot, exit price unknown.

    cumulative = peak = drawdown = 0.0
    out: List[Dict[str, Any]] = []
    for day in sorted(rows):
        row = rows[day]
        cumulative += row["pnl"]
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)
        out.append({**row, "pnl": round(row["pnl"], 2), "cumulative": round(cumulative, 2)})

    window = out[-days:] if days > 0 else []
    return {
        "days": window,
        "total_pnl": round(cumulative, 2),
        "total_trades": len(ordered),
        "trading_days": len(out),
        # Largest fall of the closed-trade equity from an earlier high (≥ 0),
        # measured at day closes.
        "max_drawdown": round(drawdown, 2),
        "best_day": max(out, key=lambda r: r["pnl"]) if out else None,
        "worst_day": min(out, key=lambda r: r["pnl"]) if out else None,
    }
