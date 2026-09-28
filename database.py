"""
SQLite persistence layer.

The database is the single source of truth for shared state — most importantly
the *active symbol*.  It runs in WAL mode with a busy timeout so that every
uvicorn worker / replica sees the same value for every request, instead of each
process answering from its own in-memory copy (which is what made the active
symbol appear to flip between instruments on its own).
"""
import json
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional

import aiosqlite

from config import settings
from models.candle import Candle
from models.signal import Signal
from models.trade import Trade

ACTIVE_SYMBOL_KEY = "active_symbol"


def _db_path() -> str:
    # Read dynamically so tests / deployments can redirect the database.
    return settings.DB_PATH


def _default_symbol_state() -> Dict[str, Any]:
    return {
        "symbol": settings.TRADING_SYMBOL,
        "symbol_name": settings.TRADING_SYMBOL_NAME,
        "version": 0,
        "changed_at": None,
        "changed_by": "default",
    }


@asynccontextmanager
async def _db():
    """Open a WAL-mode connection, commit on success, always close."""
    db = await aiosqlite.connect(_db_path())
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA busy_timeout=5000")
    try:
        yield db
        await db.commit()
    finally:
        await db.close()


async def _add_missing_columns(db: aiosqlite.Connection, table: str, columns: Dict[str, str]):
    cursor = await db.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    if not rows:
        return
    existing = {row["name"] for row in rows}
    for name, declaration in columns.items():
        if name not in existing:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


async def init_db():
    async with _db() as db:
        # Candles table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS candles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                open REAL, high REAL, low REAL, close REAL,
                volume INTEGER, timeframe TEXT,
                rsi REAL, ema_20 REAL, ema_50 REAL, ema_200 REAL,
                macd REAL, macd_signal REAL, macd_hist REAL,
                atr REAL, vwap REAL, adx REAL,
                bb_upper REAL, bb_lower REAL,
                symbol TEXT NOT NULL DEFAULT ''
            )
        """)

        # Signals table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT, direction TEXT, score INTEGER,
                confidence REAL, reasoning TEXT,
                stop_loss REAL, target_1 REAL, target_2 REAL, target_3 REAL,
                risk_reward REAL, ht_filter_passed INTEGER,
                symbol TEXT NOT NULL DEFAULT '',
                symbol_name TEXT NOT NULL DEFAULT ''
            )
        """)

        # Trades table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id TEXT PRIMARY KEY, entry_time TEXT,
                direction TEXT, entry_price REAL, quantity INTEGER,
                initial_qty INTEGER, remaining_qty INTEGER, status TEXT,
                stop_loss REAL, target_1 REAL, target_2 REAL, target_3 REAL,
                exit_t1_price REAL, exit_t1_qty INTEGER, exit_t1_time TEXT,
                exit_t2_price REAL, exit_t2_qty INTEGER, exit_t2_time TEXT,
                exit_t3_price REAL, exit_t3_qty INTEGER, exit_t3_time TEXT,
                exit_sl_price REAL, exit_sl_time TEXT,
                realized_pnl REAL, result TEXT,
                signal_score INTEGER, confidence REAL, paper_trade INTEGER,
                symbol TEXT NOT NULL DEFAULT '',
                symbol_name TEXT NOT NULL DEFAULT ''
            )
        """)

        # Shared key/value state (active symbol lives here)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS app_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL,
                updated_by TEXT NOT NULL DEFAULT 'system'
            )
        """)

        # Audit trail: who changed the active symbol, and when
        await db.execute("""
            CREATE TABLE IF NOT EXISTS symbol_changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_symbol TEXT,
                to_symbol TEXT NOT NULL,
                to_symbol_name TEXT NOT NULL,
                changed_at TEXT NOT NULL,
                changed_by TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT ''
            )
        """)

        # Migrate pre-existing databases
        await _add_missing_columns(db, "candles", {"symbol": "TEXT NOT NULL DEFAULT ''"})
        await _add_missing_columns(db, "signals", {
            "symbol": "TEXT NOT NULL DEFAULT ''",
            "symbol_name": "TEXT NOT NULL DEFAULT ''",
        })
        await _add_missing_columns(db, "trades", {
            "symbol": "TEXT NOT NULL DEFAULT ''",
            "symbol_name": "TEXT NOT NULL DEFAULT ''",
        })


# ── Generic state ────────────────────────────────────────────────

async def get_app_state(key: str) -> Optional[Dict[str, Any]]:
    async with _db() as db:
        cursor = await db.execute("SELECT * FROM app_state WHERE key = ?", (key,))
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "key": row["key"],
            "value": json.loads(row["value"]),
            "version": row["version"],
            "updated_at": row["updated_at"],
            "updated_by": row["updated_by"],
        }


async def set_app_state(key: str, value: Dict[str, Any], updated_by: str = "system") -> Dict[str, Any]:
    now = datetime.now().isoformat()
    payload = json.dumps(value)
    async with _db() as db:
        await db.execute(
            """
            INSERT INTO app_state (key, value, version, updated_at, updated_by)
            VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                version = app_state.version + 1,
                updated_at = excluded.updated_at,
                updated_by = excluded.updated_by
            """,
            (key, payload, now, updated_by),
        )
        cursor = await db.execute("SELECT * FROM app_state WHERE key = ?", (key,))
        row = await cursor.fetchone()
    return {
        "key": row["key"],
        "value": json.loads(row["value"]),
        "version": row["version"],
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by"],
    }


# ── Active symbol ────────────────────────────────────────────────

async def get_active_symbol_state() -> Dict[str, Any]:
    """Authoritative active-symbol state (shared by every process)."""
    state = await get_app_state(ACTIVE_SYMBOL_KEY)
    if state is None:
        return _default_symbol_state()
    value = state["value"]
    return {
        "symbol": value.get("symbol") or settings.TRADING_SYMBOL,
        "symbol_name": value.get("symbol_name") or settings.TRADING_SYMBOL_NAME,
        "version": state["version"],
        "changed_at": state["updated_at"],
        "changed_by": state["updated_by"],
    }


async def set_active_symbol_state(
    symbol: str,
    symbol_name: str,
    updated_by: str = "api",
    reason: str = "api_request",
) -> Dict[str, Any]:
    """
    Persist a new active symbol (monotonic version) and write an audit row.
    Both writes happen in one transaction so readers never see a half update.
    """
    now = datetime.now().isoformat()
    payload = json.dumps({"symbol": symbol, "symbol_name": symbol_name})

    async with _db() as db:
        cursor = await db.execute("SELECT * FROM app_state WHERE key = ?", (ACTIVE_SYMBOL_KEY,))
        row = await cursor.fetchone()
        if row is None:
            previous_symbol = settings.TRADING_SYMBOL
            version = 1
        else:
            previous_value = json.loads(row["value"])
            previous_symbol = previous_value.get("symbol", "")
            version = row["version"] + 1

        if row is None:
            await db.execute(
                "INSERT INTO app_state (key, value, version, updated_at, updated_by) VALUES (?, ?, ?, ?, ?)",
                (ACTIVE_SYMBOL_KEY, payload, version, now, updated_by),
            )
        else:
            await db.execute(
                "UPDATE app_state SET value = ?, version = ?, updated_at = ?, updated_by = ? WHERE key = ?",
                (payload, version, now, updated_by, ACTIVE_SYMBOL_KEY),
            )

        await db.execute(
            """
            INSERT INTO symbol_changes
                (from_symbol, to_symbol, to_symbol_name, changed_at, changed_by, reason)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (previous_symbol, symbol, symbol_name, now, updated_by, reason),
        )

    return {
        "symbol": symbol,
        "symbol_name": symbol_name,
        "version": version,
        "changed_at": now,
        "changed_by": updated_by,
    }


async def get_symbol_change_history(limit: int = 20) -> List[Dict[str, Any]]:
    async with _db() as db:
        cursor = await db.execute(
            "SELECT * FROM symbol_changes ORDER BY id DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


# ── Candles / signals / trades ───────────────────────────────────

async def save_candle(candle: Candle, symbol: str = ""):
    async with _db() as db:
        await db.execute(
            """
            INSERT INTO candles (
                timestamp, open, high, low, close, volume, timeframe,
                rsi, ema_20, ema_50, ema_200, macd, macd_signal, macd_hist,
                atr, vwap, adx, bb_upper, bb_lower, symbol
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candle.timestamp.isoformat(), candle.open, candle.high, candle.low,
                candle.close, candle.volume, candle.timeframe,
                candle.rsi, candle.ema_20, candle.ema_50, candle.ema_200,
                candle.macd, candle.macd_signal, candle.macd_hist,
                candle.atr, candle.vwap, candle.adx,
                candle.bb_upper, candle.bb_lower, symbol,
            ),
        )


async def save_signal(signal: Signal, symbol: str = "", symbol_name: str = ""):
    async with _db() as db:
        await db.execute(
            """
            INSERT INTO signals (
                timestamp, direction, score, confidence, reasoning,
                stop_loss, target_1, target_2, target_3,
                risk_reward, ht_filter_passed, symbol, symbol_name
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal.timestamp.isoformat(), signal.direction.value, signal.score,
                signal.confidence, signal.reasoning,
                signal.stop_loss, signal.target_1, signal.target_2, signal.target_3,
                signal.risk_reward, int(signal.ht_filter_passed),
                symbol, symbol_name,
            ),
        )


async def save_trade(trade: Trade):
    async with _db() as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO trades (
                id, entry_time, direction, entry_price, quantity,
                initial_qty, remaining_qty, status,
                stop_loss, target_1, target_2, target_3,
                exit_t1_price, exit_t1_qty, exit_t1_time,
                exit_t2_price, exit_t2_qty, exit_t2_time,
                exit_t3_price, exit_t3_qty, exit_t3_time,
                exit_sl_price, exit_sl_time,
                realized_pnl, result, signal_score, confidence, paper_trade,
                symbol, symbol_name
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade.id, trade.entry_time.isoformat(), trade.direction.value,
                trade.entry_price, trade.quantity, trade.initial_qty,
                trade.remaining_qty, trade.status.value, trade.stop_loss,
                trade.target_1, trade.target_2, trade.target_3,
                trade.exit_t1_price, trade.exit_t1_qty,
                trade.exit_t1_time.isoformat() if trade.exit_t1_time else None,
                trade.exit_t2_price, trade.exit_t2_qty,
                trade.exit_t2_time.isoformat() if trade.exit_t2_time else None,
                trade.exit_t3_price, trade.exit_t3_qty,
                trade.exit_t3_time.isoformat() if trade.exit_t3_time else None,
                trade.exit_sl_price,
                trade.exit_sl_time.isoformat() if trade.exit_sl_time else None,
                trade.realized_pnl, trade.result.value if trade.result else None,
                trade.signal_score, trade.confidence, int(trade.paper_trade),
                trade.symbol, trade.symbol_name,
            ),
        )


def _row_to_trade(row) -> Trade:
    return Trade(
        id=row["id"],
        entry_time=datetime.fromisoformat(row["entry_time"]),
        direction=row["direction"],
        entry_price=row["entry_price"],
        quantity=row["quantity"],
        initial_qty=row["initial_qty"],
        remaining_qty=row["remaining_qty"],
        status=row["status"],
        stop_loss=row["stop_loss"],
        target_1=row["target_1"],
        target_2=row["target_2"],
        target_3=row["target_3"],
        exit_t1_price=row["exit_t1_price"],
        exit_t1_qty=row["exit_t1_qty"],
        exit_t1_time=datetime.fromisoformat(row["exit_t1_time"]) if row["exit_t1_time"] else None,
        exit_t2_price=row["exit_t2_price"],
        exit_t2_qty=row["exit_t2_qty"],
        exit_t2_time=datetime.fromisoformat(row["exit_t2_time"]) if row["exit_t2_time"] else None,
        exit_t3_price=row["exit_t3_price"],
        exit_t3_qty=row["exit_t3_qty"],
        exit_t3_time=datetime.fromisoformat(row["exit_t3_time"]) if row["exit_t3_time"] else None,
        exit_sl_price=row["exit_sl_price"],
        exit_sl_time=datetime.fromisoformat(row["exit_sl_time"]) if row["exit_sl_time"] else None,
        realized_pnl=row["realized_pnl"],
        result=row["result"],
        signal_score=row["signal_score"],
        confidence=row["confidence"],
        paper_trade=bool(row["paper_trade"]),
        symbol=row["symbol"] or "",
        symbol_name=row["symbol_name"] or "",
    )


async def get_recent_trades(limit: int = 20) -> List[Trade]:
    async with _db() as db:
        cursor = await db.execute(
            "SELECT * FROM trades ORDER BY entry_time DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [_row_to_trade(row) for row in rows]


async def get_recent_signals(limit: int = 20) -> List[Signal]:
    async with _db() as db:
        cursor = await db.execute(
            "SELECT * FROM signals ORDER BY timestamp DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        signals: List[Signal] = []
        for row in rows:
            signal = Signal(
                timestamp=datetime.fromisoformat(row["timestamp"]),
                direction=row["direction"],
                score=row["score"],
                confidence=row["confidence"],
                reasoning=row["reasoning"],
                stop_loss=row["stop_loss"],
                target_1=row["target_1"],
                target_2=row["target_2"],
                target_3=row["target_3"],
                risk_reward=row["risk_reward"],
                ht_filter_passed=bool(row["ht_filter_passed"]),
                symbol=row["symbol"] if "symbol" in row.keys() else "",
                symbol_name=row["symbol_name"] if "symbol_name" in row.keys() else "",
            )
            signals.append(signal)
        return signals
