from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
import numpy as np

from models.candle import Candle
from models.enums import Direction
from utils import indicators
from utils.patterns import detect_candlestick_patterns
from features.options import options_analyzer
from features.news import news_monitor

class Analyzer:
    """Computes the full analytical picture for each closed candle."""
    
    def __init__(self):
        self.candles_5m: List[Candle] = []
        self.candles_15m: List[Candle] = []
        self.candles_1h: List[Candle] = []
    
    def add_tick(self, price: float, volume: int, timestamp: datetime) -> Optional[Candle]:
        """Process incoming tick, build candles for all timeframes."""
        closed_candle = None

        # Build 5m candle
        candle_5m = self._get_or_create_candle_for_tf(timestamp, price, volume, 5, self.candles_5m)
        candle_5m.high = max(candle_5m.high, price)
        candle_5m.low = min(candle_5m.low, price)
        candle_5m.close = price
        candle_5m.volume += volume
        if self._is_candle_closed_at(candle_5m, timestamp, 5):
            self._finalize_candle(candle_5m)
            closed_candle = candle_5m

        # Build 15m candle
        candle_15m = self._get_or_create_candle_for_tf(timestamp, price, volume, 15, self.candles_15m)
        candle_15m.high = max(candle_15m.high, price)
        candle_15m.low = min(candle_15m.low, price)
        candle_15m.close = price
        candle_15m.volume += volume
        if self._is_candle_closed_at(candle_15m, timestamp, 15):
            self._finalize_candle(candle_15m)
            closed_candle = candle_15m

        # Build 1h candle
        candle_1h = self._get_or_create_candle_for_tf(timestamp, price, volume, 60, self.candles_1h)
        candle_1h.high = max(candle_1h.high, price)
        candle_1h.low = min(candle_1h.low, price)
        candle_1h.close = price
        candle_1h.volume += volume
        if self._is_candle_closed_at(candle_1h, timestamp, 60):
            self._finalize_candle(candle_1h)
            closed_candle = candle_1h

        return closed_candle

    def _get_or_create_candle_for_tf(self, ts: datetime, price: float, volume: int, minutes: int, candles: List[Candle]) -> Candle:
        aligned_ts = ts.replace(minute=(ts.minute // minutes) * minutes, second=0, microsecond=0)
        if not candles or aligned_ts > candles[-1].timestamp:
            candle = Candle(
                timestamp=aligned_ts,
                open=price, high=price, low=price, close=price, volume=volume
            )
            candles.append(candle)
            return candle
        return candles[-1]

    def _is_candle_closed_at(self, candle: Candle, ts: datetime, minutes: int) -> bool:
        return ts >= candle.timestamp + timedelta(minutes=minutes)
    
    def _finalize_candle(self, candle: Candle):
        """Compute all indicators when candle closes."""
        closes = [c.close for c in self.candles_15m]
        highs = [c.high for c in self.candles_15m]
        lows = [c.low for c in self.candles_15m]
        
        # Basic indicators
        candle.rsi = indicators.rsi(closes)
        candle.ema_20 = indicators.ema(closes, 20)
        candle.ema_50 = indicators.ema(closes, 50)
        candle.ema_200 = indicators.ema(closes, 200)
        candle.macd, candle.macd_signal, candle.macd_hist = indicators.macd(closes)
        candle.atr = indicators.atr(self.candles_15m)
        candle.vwap = indicators.vwap(self.candles_15m)
        candle.adx = indicators.adx(self.candles_15m)
        candle.bb_upper, candle.bb_lower = indicators.bollinger_bands(closes)
    
    def analyze(self, candle: Candle) -> Dict[str, Any]:
        """Return complete analysis for decision engine."""
        closes = [c.close for c in self.candles_15m]
        patterns = detect_candlestick_patterns(self.candles_15m)
        
        # Advanced signals
        divergence = self._detect_rsi_divergence()
        fvg = self._detect_fair_value_gap()
        liquidity_sweep = self._detect_liquidity_sweep()
        order_block = self._detect_order_block()
        
        # Market context
        market_context = self._get_market_context()
        
        return {
            "candle": candle,
            "closes": closes,
            "patterns": patterns,
            "divergence": divergence,
            "fvg": fvg,
            "liquidity_sweep": liquidity_sweep,
            "order_block": order_block,
            "market_context": market_context,
            "higher_timeframe_trend": self._higher_timeframe_filter()
        }
    
    def _detect_rsi_divergence(self) -> Optional[str]:
        """Bullish/bearish RSI divergence."""
        if len(self.candles_15m) < 20:
            return None
        
        # Simplified: check last 5 candles for price/RSI divergence
        recent = self.candles_15m[-5:]
        price_making_lower_lows = all(recent[i].low < recent[i-1].low for i in range(1, len(recent)))
        rsi_values = [c.rsi for c in recent if c.rsi is not None]
        
        if len(rsi_values) >= 5:
            rsi_making_higher_lows = all(rsi_values[i] > rsi_values[i-1] for i in range(1, len(rsi_values)))
            if price_making_lower_lows and rsi_making_higher_lows:
                return "bullish_divergence"
        
        return None
    
    def _detect_fair_value_gap(self) -> Optional[Dict]:
        """Detect Fair Value Gap (FVG) in recent candles."""
        if len(self.candles_15m) < 3:
            return None
        
        c1, c2, c3 = self.candles_15m[-3], self.candles_15m[-2], self.candles_15m[-1]
        
        # Bullish FVG: c1.high < c3.low
        if c1.high < c3.low:
            return {"type": "bullish", "top": c3.low, "bottom": c1.high}
        # Bearish FVG: c1.low > c3.high
        if c1.low > c3.high:
            return {"type": "bearish", "top": c1.low, "bottom": c3.high}
        
        return None
    
    def _detect_liquidity_sweep(self) -> Optional[str]:
        """Detect liquidity sweep above/below recent highs/lows."""
        if len(self.candles_15m) < 10:
            return None
        
        recent_high = max(c.high for c in self.candles_15m[-10:-1])
        recent_low = min(c.low for c in self.candles_15m[-10:-1])
        current = self.candles_15m[-1]
        
        if current.high > recent_high and current.close < recent_high:
            return "sweep_high"
        if current.low < recent_low and current.close > recent_low:
            return "sweep_low"
        
        return None
    
    def _detect_order_block(self) -> Optional[Dict]:
        """Detect bullish/bearish order block."""
        if len(self.candles_15m) < 3:
            return None
        
        c1, c2, c3 = self.candles_15m[-3], self.candles_15m[-2], self.candles_15m[-1]
        
        # Bullish OB: strong bearish candle followed by strong bullish
        if c1.close < c1.open and c2.close > c2.open and c2.close > c1.open:
            return {"type": "bullish", "zone_high": c1.high, "zone_low": c1.low}
        
        # Bearish OB: strong bullish candle followed by strong bearish
        if c1.close > c1.open and c2.close < c2.open and c2.close < c1.open:
            return {"type": "bearish", "zone_high": c1.high, "zone_low": c1.low}
        
        return None
    
    async def _get_market_context(self) -> Dict[str, Any]:
        options_ctx = await options_analyzer.get_context()
        news_blackout = await news_monitor.is_blackout_active()
        active_blackouts = await news_monitor.get_active_blackouts() if news_blackout else []
    
        return {
            "pcr": options_ctx.pcr,
            "vix_regime": options_ctx.vix_regime,
            "max_pain": options_ctx.max_pain_strike,
            "call_oi": options_ctx.call_oi_total,
            "put_oi": options_ctx.put_oi_total,
            "oi_change": {
                "call": options_ctx.call_oi_change,
                "put": options_ctx.put_oi_change
            },
            "news_blackout": news_blackout,
            "active_blackouts": active_blackouts,
            "nearest_expiry": options_ctx.nearest_expiry,
            "bias": options_analyzer.get_bias(options_ctx)
        }
    
    def _higher_timeframe_filter(self) -> Direction:
        """1H trend filter — veto trades against dominant trend."""
        if len(self.candles_1h) < 50:
            return Direction.WAIT
        
        closes_1h = [c.close for c in self.candles_1h]
        ema_20_1h = indicators.ema(closes_1h, 20)
        ema_50_1h = indicators.ema(closes_1h, 50)
        
        if ema_20_1h > ema_50_1h:
            return Direction.LONG
        elif ema_20_1h < ema_50_1h:
            return Direction.SHORT
        return Direction.WAIT