"""Numbers that say whether a strategy works, and the text report."""
import csv
from collections import Counter, OrderedDict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from backtest.simulator import Result, TradeRecord
from utils.market_time import to_ist


@dataclass
class Stats:
    trades: int
    wins: int
    gross: float
    charges: float
    net: float
    profit_factor: Optional[float]   # net winners / net losers (None: no losers)
    avg_r: float                     # average net result in units of risk
    max_drawdown: float              # worst fall of net equity from a peak (Rs)

    @property
    def win_rate(self) -> float:
        return 100.0 * self.wins / self.trades if self.trades else 0.0

    @property
    def per_trade(self) -> float:
        return self.net / self.trades if self.trades else 0.0


def stats(records: List[TradeRecord]) -> Stats:
    nets = [r.net for r in records]
    won = sum(n for n in nets if n > 0)
    lost = -sum(n for n in nets if n < 0)
    equity = peak = drawdown = 0.0
    for n in nets:
        equity += n
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return Stats(
        trades=len(records),
        wins=sum(1 for n in nets if n > 0),
        gross=sum(r.gross for r in records),
        charges=sum(r.charges for r in records),
        net=sum(nets),
        profit_factor=(won / lost) if lost else None,
        avg_r=(sum(r.r_multiple for r in records) / len(records)) if records else 0.0,
        max_drawdown=drawdown,
    )


def split(records: List[TradeRecord], at: datetime):
    """Trades entered before ``at`` and from ``at`` on (naive UTC)."""
    return (
        [r for r in records if r.trade.entry_time < at],
        [r for r in records if r.trade.entry_time >= at],
    )


def by_month(records: List[TradeRecord]) -> "OrderedDict[str, Stats]":
    months: "OrderedDict[str, List[TradeRecord]]" = OrderedDict()
    for r in records:
        months.setdefault(to_ist(r.trade.entry_time).strftime("%Y-%m"), []).append(r)
    return OrderedDict((m, stats(rs)) for m, rs in months.items())


def _pf(value: Optional[float]) -> str:
    return "  n/a" if value is None else f"{value:5.2f}"


def stats_line(label: str, s: Stats) -> str:
    return (
        f"{label:<16} {s.trades:>6} {s.win_rate:>6.1f}% {s.net:>12,.0f} {s.charges:>10,.0f} "
        f"{_pf(s.profit_factor):>6} {s.avg_r:>7.2f} {s.max_drawdown:>11,.0f}"
    )


HEADER = (
    f"{'':<16} {'trades':>6} {'win%':>7} {'net Rs':>12} {'charges Rs':>10} "
    f"{'PF':>6} {'avg R':>7} {'max DD Rs':>11}"
)


def render(result: Result, start: datetime, end: datetime, midpoint: datetime) -> str:
    records = result.trades
    first, second = split(records, midpoint)
    lines = [
        f"Backtest {result.variant.name}: {to_ist(start):%Y-%m-%d} to {to_ist(end):%Y-%m-%d} (IST)",
        f"Signals (LONG/SHORT): {result.signals}   trades taken: {len(records)}",
    ]
    if result.skipped:
        lines.append("Not taken: " + ", ".join(f"{k} x{v}" for k, v in sorted(result.skipped.items())))
    lines += ["", HEADER, stats_line("all", stats(records)),
              stats_line("first half", stats(first)), stats_line("second half", stats(second))]
    if records:
        lines += ["", "By month (IST):", HEADER]
        lines += [stats_line(month, s) for month, s in by_month(records).items()]
        reasons = Counter(r.exit_reason for r in records)
        lines += ["", "Exits: " + ", ".join(f"{k} x{v}" for k, v in reasons.most_common())]
    return "\n".join(lines)


#: Fewer trades than this in a half and a difference is indistinguishable from luck.
MIN_TRADES_FOR_VERDICT = 30


def render_ablation(results: Dict[str, Result], midpoint: datetime) -> str:
    """Each component switched off, against the baseline, in both halves.

    A component earns its place only if the strategy is worse without it in
    *both* halves; one that only helps in one half is noise.
    """
    base = results["baseline"]
    b1, b2 = (stats(part) for part in split(base.trades, midpoint))
    lines = [
        "Ablation: net Rs with one ingredient switched off (chg = change vs baseline)",
        f"{'variant':<16} {'trades':>6} {'net 1st half':>14} {'chg':>10} {'net 2nd half':>14} {'chg':>10}  verdict",
        f"{'baseline':<16} {len(base.trades):>6} {b1.net:>14,.0f} {'':>10} {b2.net:>14,.0f}",
    ]
    for name, result in results.items():
        if name == "baseline":
            continue
        s1, s2 = (stats(part) for part in split(result.trades, midpoint))
        d1, d2 = s1.net - b1.net, s2.net - b2.net
        if min(s1.trades, s2.trades, b1.trades, b2.trades) < MIN_TRADES_FOR_VERDICT:
            verdict = "too few trades"
        elif d1 < 0 and d2 < 0:
            verdict = "helps (keep)"
        elif d1 > 0 and d2 > 0:
            verdict = "hurts (drop?)"
        else:
            verdict = "unclear"
        lines.append(
            f"{name:<16} {len(result.trades):>6} {s1.net:>14,.0f} {d1:>+10,.0f} {s2.net:>14,.0f} {d2:>+10,.0f}  {verdict}"
        )
    return "\n".join(lines)


def write_trades(path: Path, records: List[TradeRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "entry_time_ist", "exit_time_ist", "direction", "qty", "entry", "stop",
            "exit", "exit_reason", "score", "gross", "charges", "net", "r",
        ])
        for r in records:
            t = r.trade
            writer.writerow([
                f"{to_ist(t.entry_time):%Y-%m-%d %H:%M:%S}",
                f"{to_ist(t.exit_time):%Y-%m-%d %H:%M:%S}" if t.exit_time else "",
                t.direction.value, t.initial_qty, t.entry_price, t.stop_loss,
                t.exit_price, r.exit_reason, t.signal_score,
                round(r.gross, 2), round(r.charges, 2), round(r.net, 2), round(r.r_multiple, 3),
            ])
