"""
Backtest the bot's strategy on 1-minute history.

    python -m backtest fetch  --from 2025-01-01 --to 2026-10-23
    python -m backtest run    --from 2025-01-01 --to 2026-10-23
    python -m backtest ablate --from 2025-01-01 --to 2026-10-23

Dates are IST calendar days. ``fetch`` needs a valid Fyers login; the other
commands read the cached CSV (data/backtest/) and never touch the network.
Use ``--synthetic N`` to try the tool on N days of generated prices.
"""
import argparse
import asyncio
import time
from datetime import datetime, timedelta
from pathlib import Path

from backtest import data
from backtest.costs import PRESETS, Slippage
from backtest.replay import replay
from backtest.report import render, render_ablation, write_trades
from backtest.simulator import COMPONENTS, Settings, Variant, simulate
from config import settings

RESULTS_DIR = data.CACHE_DIR / "results"


def _ist_day(text: str) -> datetime:
    """IST calendar day -> naive UTC midnight of that IST day."""
    return datetime.strptime(text, "%Y-%m-%d") - timedelta(hours=5, minutes=30)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m backtest", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=("fetch", "run", "ablate"))
    p.add_argument("--symbol", default=settings.TRADING_SYMBOL)
    p.add_argument("--from", dest="start", help="first IST day, YYYY-MM-DD")
    p.add_argument("--to", dest="end", help="last IST day, YYYY-MM-DD (default: today)")
    p.add_argument("--warmup-days", type=int, default=10,
                   help="calendar days replayed before trading starts, so indicators are ready (default 10)")
    p.add_argument("--capital", type=float, default=settings.CAPITAL)
    p.add_argument("--lot-size", type=int, default=None,
                   help="override the lot (default: 1 for -EQ, LOT_SIZE otherwise)")
    p.add_argument("--costs", choices=sorted(PRESETS), default=None,
                   help="charges preset (default: equity for -EQ, futures otherwise)")
    p.add_argument("--slippage", type=float, default=0.01, help="adverse fill, %% of price (default 0.01)")
    p.add_argument("--synthetic", type=int, default=0, metavar="DAYS",
                   help="use DAYS of generated prices instead of real history")
    return p


async def _main(args) -> int:
    end = _ist_day(args.end) + timedelta(days=1) if args.end else datetime.utcnow()
    if args.synthetic:
        minutes = data.synthetic(args.synthetic)
        start = minutes[0].timestamp
        end = minutes[-1].timestamp
        trade_from = start + timedelta(days=args.warmup_days)
    else:
        if not args.start:
            raise SystemExit("--from is required")
        start = _ist_day(args.start)
        if args.command == "fetch":
            path = await data.fetch(args.symbol, start - timedelta(days=args.warmup_days), end)
            print(f"Saved {path}")
            return 0
        path = data.cache_path(args.symbol)
        if not path.exists():
            raise SystemExit(f"No cached history at {path}: run `python -m backtest fetch` first")
        trade_from = start
        minutes = data.load_csv(path, start - timedelta(days=args.warmup_days), end)
        if not minutes:
            raise SystemExit(f"{path} has no candles in that range: fetch it first")

    began = time.monotonic()
    print(f"Replaying {len(minutes):,} one-minute candles of {args.symbol}...")
    rep = await replay(minutes)
    print(f"  {len(rep.decisions):,} closed 15m candles in {time.monotonic() - began:.0f}s")

    config = Settings(
        symbol=args.symbol, capital=args.capital, lot_size=args.lot_size,
        costs=PRESETS[args.costs] if args.costs else None,
        slippage=Slippage(args.slippage), trade_from=trade_from,
    )
    last = minutes[-1].timestamp
    midpoint = trade_from + (last - trade_from) / 2
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if args.command == "run":
        result = await simulate(rep, Variant(), config)
        print()
        print(render(result, trade_from, last, midpoint))
        out = RESULTS_DIR / f"trades_{stamp}.csv"
        write_trades(out, result.trades)
        print(f"\nTrades: {out}")
        return 0

    results = {"baseline": await simulate(rep, Variant(), config)}
    for component in COMPONENTS:
        variant = Variant.without(component)
        results[variant.name] = await simulate(rep, variant, config)
    print()
    print(render(results["baseline"], trade_from, last, midpoint))
    print()
    print(render_ablation(results, midpoint))
    return 0


def main() -> int:
    return asyncio.run(_main(_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
