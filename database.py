import aiosqlite
from datetime import datetime
from typing import List, Optional
from models.candle import Candle
from models.signal import Signal
from models.trade import Trade
from config import settings

DB_PATH = settings.DB_PATH

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
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
                bb_upper REAL, bb_lower REAL
            )
        """)
        
        # Signals table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT, direction TEXT, score INTEGER,
                confidence REAL, reasoning TEXT,
                stop_loss REAL, target_1 REAL, target_2 REAL, target_3 REAL,
                risk_reward REAL, ht_filter_passed INTEGER
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
                signal_score INTEGER, confidence REAL, paper_trade INTEGER
            )
        """)
        
        await db.commit()

async def save_candle(candle: Candle):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO candles VALUES (
                NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
        """, (
            candle.timestamp.isoformat(), candle.open, candle.high, candle.low,
            candle.close, candle.volume, candle.timeframe,
            candle.rsi, candle.ema_20, candle.ema_50, candle.ema_200,
            candle.macd, candle.macd_signal, candle.macd_hist,
            candle.atr, candle.vwap, candle.adx,
            candle.bb_upper, candle.bb_lower
        ))
        await db.commit()

async def save_signal(signal: Signal):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO signals VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            signal.timestamp.isoformat(), signal.direction.value, signal.score,
            signal.confidence, signal.reasoning,
            signal.stop_loss, signal.target_1, signal.target_2, signal.target_3,
            signal.risk_reward, int(signal.ht_filter_passed)
        ))
        await db.commit()

async def save_trade(trade: Trade):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR REPLACE INTO trades VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
        """, (
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
            trade.signal_score, trade.confidence, int(trade.paper_trade)
        ))
        await db.commit()

async def get_recent_trades(limit: int = 20) -> List[Trade]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM trades ORDER BY entry_time DESC LIMIT ?", (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
            trades = []
            for row in rows:
                trade = Trade(
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
                    realized_pnl=row["realized_pnl"],
                    signal_score=row["signal_score"],
                    confidence=row["confidence"],
                    paper_trade=bool(row["paper_trade"])
                )
                trades.append(trade)
            return trades

async def get_recent_signals(limit: int = 20) -> List[Signal]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM signals ORDER BY timestamp DESC LIMIT ?", (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
            signals = []
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
                    ht_filter_passed=bool(row["ht_filter_passed"])
                )
                signals.append(signal)
            return signals