from typing import Dict, Any, Optional
from datetime import datetime
from models.signal import Signal
from models.enums import Direction
from config import settings

class DecisionEngine:
    """
    Pure-Python rules-based scoring engine.
    Scores LONG and SHORT independently on ~100-point scale.
    """
    
    def __init__(self):
        self.min_threshold = settings.MIN_SCORE_THRESHOLD
    
    def evaluate(self, analysis: Dict[str, Any]) -> Signal:
        """Score both directions and return the winning signal."""
        candle = analysis["candle"]
        patterns = analysis["patterns"]
        market_context = analysis["market_context"]
        ht_filter = analysis["higher_timeframe_trend"]
        
        long_score = self._score_long(analysis)
        short_score = self._score_short(analysis)
        
        # Determine direction
        if long_score > short_score and long_score >= self.min_threshold:
            direction = Direction.LONG
            score = long_score
        elif short_score > long_score and short_score >= self.min_threshold:
            direction = Direction.SHORT
            score = short_score
        else:
            return self._wait_signal(candle)
        
        # Higher timeframe veto
        if ht_filter != Direction.WAIT and ht_filter != direction:
            return self._wait_signal(
                candle, 
                reason=f"HT filter veto: {ht_filter.value} trend, signal was {direction.value}"
            )
        
        # Calculate targets based on ATR
        atr = candle.atr or 50
        if direction == Direction.LONG:
            stop_loss = candle.low - (0.8 * atr)
            target_1 = candle.close + (2 * atr)
            target_2 = candle.close + (3 * atr)
            target_3 = candle.close + (4 * atr)
        else:
            stop_loss = candle.high + (0.8 * atr)
            target_1 = candle.close - (2 * atr)
            target_2 = candle.close - (3 * atr)
            target_3 = candle.close - (4 * atr)
        
        risk = abs(candle.close - stop_loss)
        reward = abs(target_1 - candle.close)
        risk_reward = reward / risk if risk > 0 else 0
        
        confidence = score / 100.0
        
        reasoning = self._build_reasoning(direction, score, analysis)
        
        return Signal(
            timestamp=datetime.now(),
            direction=direction,
            score=score,
            confidence=confidence,
            reasoning=reasoning,
            trend_score=long_score if direction == Direction.LONG else short_score,
            stop_loss=round(stop_loss, 2),
            target_1=round(target_1, 2),
            target_2=round(target_2, 2),
            target_3=round(target_3, 2),
            risk_reward=round(risk_reward, 2),
            ht_filter_passed=True
        )
    
    def _score_long(self, analysis: Dict[str, Any]) -> int:
        """Score LONG setup (max ~100 points)."""
        candle = analysis["candle"]
        patterns = analysis["patterns"]
        score = 0
        
        # Trend alignment (max 25)
        if candle.ema_20 and candle.ema_50 and candle.ema_200:
            if candle.ema_20 > candle.ema_50 > candle.ema_200:
                score += 25  # Strong uptrend
            elif candle.ema_20 > candle.ema_50:
                score += 15
            elif candle.close > candle.ema_200:
                score += 5
        
        # VWAP position (max 10)
        if candle.vwap and candle.close > candle.vwap:
            score += 10
        
        # RSI momentum (max 15)
        if candle.rsi:
            if 50 <= candle.rsi <= 65:
                score += 15  # Ideal momentum zone
            elif 40 <= candle.rsi < 50:
                score += 8
            elif candle.rsi >= 70:
                score -= 10  # Penalize overbought
        
        # MACD (max 10)
        if candle.macd_hist and candle.macd_hist > 0:
            score += 10
        
        # Volume (max 10)
        # Simplified: would compare to 20-bar average
        score += 5  # Placeholder
        
        # Candlestick patterns (max 15)
        bullish_patterns = ["hammer", "engulfing_bullish", "morning_star", "piercing"]
        for p in patterns:
            if p in bullish_patterns:
                score += 15
                break
        
        # Advanced signals (max 15)
        if analysis.get("divergence") == "bullish_divergence":
            score += 8
        if analysis.get("fvg", {}).get("type") == "bullish":
            score += 4
        if analysis.get("liquidity_sweep") == "sweep_low":
            score += 3
        
        return max(0, score)
    
    def _score_short(self, analysis: Dict[str, Any]) -> int:
        """Score SHORT setup (mirror of LONG)."""
        candle = analysis["candle"]
        patterns = analysis["patterns"]
        score = 0
        
        # Trend alignment (max 25)
        if candle.ema_20 and candle.ema_50 and candle.ema_200:
            if candle.ema_20 < candle.ema_50 < candle.ema_200:
                score += 25
            elif candle.ema_20 < candle.ema_50:
                score += 15
            elif candle.close < candle.ema_200:
                score += 5
        
        # VWAP (max 10)
        if candle.vwap and candle.close < candle.vwap:
            score += 10
        
        # RSI (max 15)
        if candle.rsi:
            if 35 <= candle.rsi <= 50:
                score += 15
            elif 50 < candle.rsi <= 60:
                score += 8
            elif candle.rsi <= 30:
                score -= 10
        
        # MACD (max 10)
        if candle.macd_hist and candle.macd_hist < 0:
            score += 10
        
        # Volume (max 10)
        score += 5
        
        # Patterns (max 15)
        bearish_patterns = ["shooting_star", "engulfing_bearish", "evening_star", "dark_cloud"]
        for p in patterns:
            if p in bearish_patterns:
                score += 15
                break
        
        # Advanced signals (max 15)
        if analysis.get("divergence") == "bearish_divergence":
            score += 8
        if analysis.get("fvg", {}).get("type") == "bearish":
            score += 4
        if analysis.get("liquidity_sweep") == "sweep_high":
            score += 3
        
        return max(0, score)
    
    def _wait_signal(self, candle, reason: str = "Score below threshold") -> Signal:
        return Signal(
            timestamp=datetime.now(),
            direction=Direction.WAIT,
            score=0,
            confidence=0.0,
            reasoning=reason,
            stop_loss=candle.close,
            target_1=candle.close,
            target_2=candle.close,
            target_3=candle.close,
            risk_reward=0.0
        )
    
    def _build_reasoning(self, direction: Direction, score: int, analysis: Dict) -> str:
        candle = analysis["candle"]
        return (
            f"{direction.value} signal scored {score}/100. "
            f"Price: {candle.close}, RSI: {candle.rsi:.1f}, "
            f"EMA20/50/200: {candle.ema_20:.1f}/{candle.ema_50:.1f}/{candle.ema_200:.1f}. "
            f"Patterns: {', '.join(analysis['patterns'])}. "
            f"HT filter: {analysis['higher_timeframe_trend'].value}."
        )