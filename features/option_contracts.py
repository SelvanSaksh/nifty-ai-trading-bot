"""
Index option contracts, read from the Fyers symbol master.

Bank Nifty cannot be bought: it is an index. The bot reads its signal, stop
and targets on the index chart and buys a call option of it. This module
picks that call:

- the expiry is the nearest one in the master file that is at least
  ``OPTION_MIN_DAYS_TO_EXPIRY`` calendar days (IST) away. Expiry dates are
  never computed: Bank Nifty has monthly contracts only, normally on the last
  Tuesday, and holidays move them (Jan 2027 expires on Monday the 25th);
- the strike is the listed strike nearest the index price (ATM). On an exact
  tie the lower strike is taken, which for a call is the in-the-money one;
- the lot size and tick size are the contract's own, from the same file.

Symbol master: https://public.fyers.in/sym_details/NSE_FO.csv, one row per
contract, no header. The columns used (0-based):

    2  instrument type (14 = index option)
    3  lot size                 4  tick size
    8  expiry, epoch seconds    9  symbol ticker (NSE:BANKNIFTY26DEC59800CE)
    13 underlying (BANKNIFTY)   15 strike           16 option type (CE / PE)

The file is downloaded once per IST day and kept in ``data/`` so a restart
the same day does not download it again. A copy from an earlier day is
never used: contracts and lots change at every expiry.
"""
import asyncio
import csv
import io
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

import aiohttp

from config import settings
from utils.market_time import to_ist

#: Index the bot trades with options -> its underlying in the symbol master.
#: Only Bank Nifty: the user trades its calls; other indices keep their
#: existing (paper futures) handling.
OPTION_UNDERLYINGS: Dict[str, str] = {
    "NSE:NIFTYBANK-INDEX": "BANKNIFTY",
}

INDEX_OPTION_TYPE = "14"
CALL = "CE"

#: Downloads the master file's text.
FetchMaster = Callable[[], Awaitable[str]]


class OptionContractError(Exception):
    """No contract can be chosen (no master file, nothing listed, ...)."""


@dataclass(frozen=True)
class OptionContract:
    symbol: str          # NSE:BANKNIFTY26DEC59800CE
    underlying: str      # BANKNIFTY
    option_type: str     # CE
    strike: float
    expiry: datetime     # naive UTC, as the master file gives it
    lot_size: int
    tick_size: float


def trades_options(index_symbol: str) -> bool:
    """True when ``index_symbol`` is traded through its options."""
    return index_symbol in OPTION_UNDERLYINGS


def parse_master(text: str, underlying: str, option_type: str = CALL) -> List[OptionContract]:
    """Every ``underlying`` index option of ``option_type`` in the master
    file's text. Rows that do not parse are skipped, never guessed."""
    contracts = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 17:
            continue
        if row[13].strip() != underlying or row[16].strip() != option_type:
            continue
        if row[2].strip() != INDEX_OPTION_TYPE:
            continue
        try:
            lot = int(float(row[3]))
            tick = float(row[4])
            expiry = datetime.utcfromtimestamp(int(float(row[8])))
            strike = float(row[15])
        except (TypeError, ValueError, OverflowError, OSError):
            continue
        symbol = row[9].strip()
        if lot <= 0 or tick <= 0 or strike <= 0 or not symbol.startswith("NSE:"):
            continue
        contracts.append(OptionContract(
            symbol=symbol, underlying=underlying, option_type=option_type,
            strike=strike, expiry=expiry, lot_size=lot, tick_size=tick,
        ))
    return contracts


def choose_expiry(
    contracts: List[OptionContract], now_utc: datetime, min_days: int
) -> datetime:
    """The nearest listed expiry at least ``min_days`` IST calendar days after
    today (and not yet expired)."""
    today = to_ist(now_utc).date()
    expiries = sorted({c.expiry for c in contracts if c.expiry > now_utc})
    for expiry in expiries:
        if (to_ist(expiry).date() - today).days >= min_days:
            return expiry
    raise OptionContractError(
        f"No expiry at least {min_days} day(s) away in the symbol master "
        f"(listed: {', '.join(to_ist(e).date().isoformat() for e in expiries) or 'none'})"
    )


def choose_atm(contracts: List[OptionContract], expiry: datetime, price: float) -> OptionContract:
    """The contract of ``expiry`` whose strike is nearest ``price`` (the
    lower strike on an exact tie)."""
    listed = [c for c in contracts if c.expiry == expiry]
    if not listed:
        raise OptionContractError(f"No contract listed for the {to_ist(expiry).date()} expiry")
    return min(listed, key=lambda c: (abs(c.strike - price), c.strike))


async def download_master() -> str:
    timeout = aiohttp.ClientTimeout(total=90)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(settings.FYERS_SYMBOL_MASTER_URL) as resp:
            if resp.status != 200:
                raise OptionContractError(
                    f"Symbol master download failed: HTTP {resp.status}"
                )
            return await resp.text()


class OptionBook:
    """The day's contracts of each traded underlying, from the master file."""

    def __init__(
        self,
        fetch: FetchMaster = download_master,
        cache_path: Optional[Path] = None,
    ):
        self._fetch = fetch
        self._cache_path = cache_path
        self._lock = asyncio.Lock()
        # (underlying, option type) -> (IST day loaded, contracts)
        self._loaded: Dict[Tuple[str, str], Tuple[str, List[OptionContract]]] = {}
        self._text_day: Optional[str] = None
        self._text: Optional[str] = None

    def _path(self) -> Path:
        return self._cache_path or Path(settings.DB_PATH).parent / "NSE_FO.csv"

    async def _master_text(self, day: str) -> str:
        if self._text is not None and self._text_day == day:
            return self._text
        path = self._path()
        text = None
        try:
            if path.exists():
                saved_day = to_ist(datetime.utcfromtimestamp(path.stat().st_mtime)).date().isoformat()
                if saved_day == day:
                    text = await asyncio.to_thread(path.read_text, "utf-8")
        except OSError as exc:
            print(f"[OPTIONS] Saved symbol master unreadable: {exc}")
        if text is None:
            try:
                text = await self._fetch()
            except OptionContractError:
                raise
            except Exception as exc:
                raise OptionContractError(f"Symbol master download failed: {exc}") from exc
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(path.write_text, text, "utf-8")
            except OSError as exc:
                print(f"[OPTIONS] Symbol master not saved: {exc}")
        self._text, self._text_day = text, day
        return text

    async def contracts(
        self, underlying: str, option_type: str = CALL, now_utc: Optional[datetime] = None
    ) -> List[OptionContract]:
        """Today's listed ``underlying`` options of ``option_type``."""
        now_utc = now_utc or datetime.utcnow()
        day = to_ist(now_utc).date().isoformat()
        key = (underlying, option_type)
        async with self._lock:
            loaded = self._loaded.get(key)
            if loaded is not None and loaded[0] == day:
                return loaded[1]
            text = await self._master_text(day)
            found = await asyncio.to_thread(parse_master, text, underlying, option_type)
            if not found:
                raise OptionContractError(
                    f"The symbol master lists no {underlying} {option_type} options"
                )
            self._loaded[key] = (day, found)
            return found

    async def atm_call(
        self, index_symbol: str, index_price: float, now_utc: Optional[datetime] = None
    ) -> OptionContract:
        """The at-the-money call of ``index_symbol`` at ``index_price``."""
        underlying = OPTION_UNDERLYINGS.get(index_symbol)
        if underlying is None:
            raise OptionContractError(f"{index_symbol} is not traded with options")
        now_utc = now_utc or datetime.utcnow()
        listed = await self.contracts(underlying, CALL, now_utc)
        expiry = choose_expiry(listed, now_utc, settings.OPTION_MIN_DAYS_TO_EXPIRY)
        return choose_atm(listed, expiry, index_price)


def describe(contract: OptionContract) -> str:
    """``BANKNIFTY 29 Dec 2026 59800 CE (lot 30)``."""
    strike = f"{contract.strike:.0f}" if contract.strike.is_integer() else f"{contract.strike}"
    return (
        f"{contract.underlying} {to_ist(contract.expiry).strftime('%d %b %Y')} "
        f"{strike} {contract.option_type} (lot {contract.lot_size})"
    )


#: Shared by the engine (one master file a day for the process).
option_book = OptionBook()
