import numpy as np
import pandas as pd
from typing import List
from models.candle import Candle

def rsi(closes: List[float], period: int = 14) -> float:
    """Relative Strength Index."""
    if len(closes) < period + 1:
        return 50.0
    
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    
    avg_gain = np.mean(gains[-period:])
    avg_loss = np.mean(losses[-period:])
    
    if avg_loss == 0:
        return 100.0
    
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def ema(closes: List[float], period: int) -> float:
    """Exponential Moving Average."""
    if len(closes) < period:
        return closes[-1] if closes else 0.0
    
    weights = np.exp(np.linspace(-1., 0., period))
    weights /= weights.sum()
    return np.dot(closes[-period:], weights[::-1])

def macd(closes: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple:
    """MACD line, signal line, histogram."""
    if len(closes) < slow + signal:
        return 0.0, 0.0, 0.0
    
    ema_fast = pd.Series(closes).ewm(span=fast, adjust=False).mean().iloc[-1]
    ema_slow = pd.Series(closes).ewm(span=slow, adjust=False).mean().iloc[-1]
    macd_line = ema_fast - ema_slow
    
    # Signal line (EMA of MACD)
    macd_series = pd.Series(closes).ewm(span=fast).mean() - pd.Series(closes).ewm(span=slow).mean()
    signal_line = macd_series.ewm(span=signal, adjust=False).mean().iloc[-1]
    histogram = macd_line - signal_line
    
    return macd_line, signal_line, histogram

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
    """Volume Weighted Average Price (session-based)."""
    if not candles:
        return 0.0
    
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