import numpy as np
import pandas as pd
from typing import List, Optional, Tuple
from models.candle import Candle
from utils.market_time import to_ist

def _smoothed_series(values: np.ndarray, period: int, alpha: float) -> np.ndarray:
    """A recursive average seeded with the mean of the first ``period``
    values, then avg = avg + alpha * (x - avg) for every later x. Element i
    is the average through ``values[period - 1 + i]``."""
    seeded = np.concatenate(([values[:period].mean()], values[period:]))
    return pd.Series(seeded).ewm(alpha=alpha, adjust=False).mean().to_numpy()


def _smoothed(values: np.ndarray, period: int, alpha: float) -> float:
    """Last value of ``_smoothed_series``."""
    return float(_smoothed_series(values, period, alpha)[-1])


def rsi(closes: List[float], period: int = 14) -> Optional[float]:
    """Wilder's RSI, the standard one and the chart's
    (trading_web_flutter lib/src/util/indicators.dart): average gain and loss
    seeded with the mean of the first ``period`` changes, then smoothed by
    Wilder's 1/period. None until ``period`` + 1 closes.

    It used to average only the last ``period`` changes (Cutler's RSI) and
    report 50 on short series, so it disagreed with the chart.
    """
    if period <= 0 or len(closes) <= period:
        return None
    deltas = np.diff(np.asarray(closes, dtype=float))
    avg_gain = _smoothed(np.clip(deltas, 0, None), period, 1 / period)
    avg_loss = _smoothed(np.clip(-deltas, 0, None), period, 1 / period)
    if avg_loss == 0:
        return 50.0 if avg_gain == 0 else 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def ema(closes: List[float], period: int) -> Optional[float]:
    """Standard EMA (multiplier 2 / (period + 1)) over the whole series,
    seeded with the SMA of the first ``period`` closes: the chart's formula
    (trading_web_flutter lib/src/util/indicators.dart). None until
    ``period`` closes.

    It used to be a weighted mean of the last ``period`` closes only, close
    to an SMA (EMA 200 was ~55 points off a real EMA 200 on Nifty-like
    prices), and the last close on short series. The seed fades: after
    ~5 x period closes the result no longer depends on where the series
    starts (see analyzer.WARMUP_DAYS).
    """
    if period <= 0 or len(closes) < period:
        return None
    return _smoothed(np.asarray(closes, dtype=float), period, 2 / (period + 1))

def macd(
    closes: List[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Standard MACD (TradingView's ta.macd): line = EMA(fast) - EMA(slow),
    signal = EMA(signal) of the line, histogram = line - signal. Every EMA
    is ``ema()``'s (SMA seed, multiplier 2 / (n + 1)). (None, None, None)
    until slow + signal - 1 closes, when the signal line has its seed.

    It used to take the line from EMAs seeded at the first close and the
    signal from a *different* MACD series (pandas' adjust=True weights),
    so the histogram was the gap between two unrelated lines; and it gave
    0.0 on short series.
    """
    if min(fast, slow, signal) <= 0 or len(closes) < slow + signal - 1:
        return None, None, None
    values = np.asarray(closes, dtype=float)
    # Both EMAs aligned on the closes from index slow - 1 on.
    ema_fast = _smoothed_series(values, fast, 2 / (fast + 1))[slow - fast:]
    ema_slow = _smoothed_series(values, slow, 2 / (slow + 1))
    line = ema_fast - ema_slow
    signal_line = _smoothed(line, signal, 2 / (signal + 1))
    return float(line[-1]), signal_line, float(line[-1]) - signal_line

def atr(candles: List[Candle], period: int = 14) -> float:
    """Average True Range."""
    if len(candles) < period + 1:
        return candles[-1].high - candles[-1].low if candles else 0.0
    
    tr_list = []
    for i in range(1, len(candles)):
        high = candles[i].high
        low = candles[i].low
        prev_close = candles[i-1].close
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_list.append(tr)
    
    return np.mean(tr_list[-period:])

def vwap(candles: List[Candle]) -> float:
    """Session VWAP: from the first candle of the newest candle's IST trading
    day (candle times are naive UTC). It used to run over the whole buffer,
    across days, which is not VWAP. ``candles`` is oldest first."""
    if not candles:
        return 0.0

    session_day = to_ist(candles[-1].timestamp).date()
    start = len(candles) - 1
    while start > 0 and to_ist(candles[start - 1].timestamp).date() == session_day:
        start -= 1
    candles = candles[start:]

    typical_prices = [(c.high + c.low + c.close) / 3 for c in candles]
    volumes = [c.volume for c in candles]
    
    cum_tp_vol = sum(tp * vol for tp, vol in zip(typical_prices, volumes))
    cum_vol = sum(volumes)
    
    return cum_tp_vol / cum_vol if cum_vol > 0 else typical_prices[-1]

def adx(candles: List[Candle], period: int = 14) -> float:
    """Average Directional Index."""
    if len(candles) < period * 2:
        return 25.0  # Neutral
    
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]
    
    plus_dm = []
    minus_dm = []
    tr_list = []
    
    for i in range(1, len(candles)):
        up_move = highs[i] - highs[i-1]
        down_move = lows[i-1] - lows[i]
        
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0)
        
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1])
        )
        tr_list.append(tr)
    
    atr_val = np.mean(tr_list[-period:])
    plus_di = 100 * np.mean(plus_dm[-period:]) / atr_val if atr_val > 0 else 0
    minus_di = 100 * np.mean(minus_dm[-period:]) / atr_val if atr_val > 0 else 0
    
    dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di) if (plus_di + minus_di) > 0 else 0
    return np.mean([dx] * period)  # Smoothed DX

def bollinger_bands(closes: List[float], period: int = 20, std_dev: int = 2) -> tuple:
    """Upper and lower Bollinger Bands."""
    if len(closes) < period:
        return closes[-1] * 1.02, closes[-1] * 0.98 if closes else (0, 0)
    
    sma = np.mean(closes[-period:])
    std = np.std(closes[-period:])
    return sma + (std_dev * std), sma - (std_dev * std)