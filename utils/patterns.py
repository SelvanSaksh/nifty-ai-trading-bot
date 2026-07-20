"""
Candlestick pattern detection and price action analysis.
Detects 12+ patterns including advanced signals.
"""
from typing import List, Optional, Dict, Any
from dataclasses import dataclass

from models.candle import Candle
from utils.helpers import (
    is_bullish_candle, is_bearish_candle, candle_body, 
    candle_range, upper_wick, lower_wick, is_doji, safe_divide
)


@dataclass
class PatternResult:
    name: str
    bullish: bool
    strength: int  # 1-10
    reliability: str  # weak, moderate, strong
    description: str


# ── Pattern Detection Functions ─────────────────────────────────

def is_hammer(candles: List[Candle]) -> bool:
    """
    Hammer: Small body at top, long lower wick, little/no upper wick.
    Bullish reversal at bottom of downtrend.
    """
    if len(candles) < 1:
        return False
    
    c = candles[-1]
    body = candle_body(c.open, c.close)
    range_ = candle_range(c.high, c.low)
    l_wick = lower_wick(c.low, c.open, c.close)
    u_wick = upper_wick(c.high, c.open, c.close)
    
    if range_ == 0:
        return False
    
    body_pct = body / range_
    lower_wick_pct = l_wick / range_
    upper_wick_pct = u_wick / range_
    
    # Body in upper third, long lower wick, small upper wick
    return (
        is_bullish_candle(c.open, c.close) and
        body_pct < 0.3 and
        lower_wick_pct > 0.6 and
        upper_wick_pct < 0.1 and
        c.close > (c.high + c.low) / 2  # Close in upper half
    )


def is_shooting_star(candles: List[Candle]) -> bool:
    """
    Shooting Star: Small body at bottom, long upper wick, little/no lower wick.
    Bearish reversal at top of uptrend.
    """
    if len(candles) < 1:
        return False
    
    c = candles[-1]
    body = candle_body(c.open, c.close)
    range_ = candle_range(c.high, c.low)
    l_wick = lower_wick(c.low, c.open, c.close)
    u_wick = upper_wick(c.high, c.open, c.close)
    
    if range_ == 0:
        return False
    
    body_pct = body / range_
    lower_wick_pct = l_wick / range_
    upper_wick_pct = u_wick / range_
    
    return (
        is_bearish_candle(c.open, c.close) and
        body_pct < 0.3 and
        upper_wick_pct > 0.6 and
        lower_wick_pct < 0.1 and
        c.close < (c.high + c.low) / 2  # Close in lower half
    )


def is_engulfing_bullish(candles: List[Candle]) -> bool:
    """
    Bullish Engulfing: Current bullish candle completely engulfs previous bearish candle.
    """
    if len(candles) < 2:
        return False
    
    prev, curr = candles[-2], candles[-1]
    
    return (
        is_bearish_candle(prev.open, prev.close) and
        is_bullish_candle(curr.open, curr.close) and
        curr.open < prev.close and  # Open below previous close
        curr.close > prev.open       # Close above previous open
    )


def is_engulfing_bearish(candles: List[Candle]) -> bool:
    """
    Bearish Engulfing: Current bearish candle completely engulfs previous bullish candle.
    """
    if len(candles) < 2:
        return False
    
    prev, curr = candles[-2], candles[-1]
    
    return (
        is_bullish_candle(prev.open, prev.close) and
        is_bearish_candle(curr.open, curr.close) and
        curr.open > prev.close and  # Open above previous close
        curr.close < prev.open       # Close below previous open
    )


def is_morning_star(candles: List[Candle]) -> bool:
    """
    Morning Star: Bearish -> Small/Doji -> Bullish.
    Bullish reversal pattern.
    """
    if len(candles) < 3:
        return False
    
    c1, c2, c3 = candles[-3], candles[-2], candles[-1]
    
    # First candle: strong bearish
    c1_bearish = is_bearish_candle(c1.open, c1.close)
    c1_body = candle_body(c1.open, c1.close)
    
    # Second candle: small body (star/doji)
    c2_body = candle_body(c2.open, c2.close)
    c2_range = candle_range(c2.high, c2.low)
    c2_small = c2_body / c2_range < 0.3 if c2_range > 0 else False
    
    # Third candle: strong bullish, closes into first candle body
    c3_bullish = is_bullish_candle(c3.open, c3.close)
    c3_body = candle_body(c3.open, c3.close)
    c3_closes_into_c1 = c3.close > (c1.open + c1.close) / 2
    
    return (
        c1_bearish and c1_body > 0 and
        c2_small and
        c3_bullish and c3_body > c2_body and
        c3_closes_into_c1
    )


def is_evening_star(candles: List[Candle]) -> bool:
    """
    Evening Star: Bullish -> Small/Doji -> Bearish.
    Bearish reversal pattern.
    """
    if len(candles) < 3:
        return False
    
    c1, c2, c3 = candles[-3], candles[-2], candles[-1]
    
    c1_bullish = is_bullish_candle(c1.open, c1.close)
    c1_body = candle_body(c1.open, c1.close)
    
    c2_body = candle_body(c2.open, c2.close)
    c2_range = candle_range(c2.high, c2.low)
    c2_small = c2_body / c2_range < 0.3 if c2_range > 0 else False
    
    c3_bearish = is_bearish_candle(c3.open, c3.close)
    c3_body = candle_body(c3.open, c3.close)
    c3_closes_into_c1 = c3.close < (c1.open + c1.close) / 2
    
    return (
        c1_bullish and c1_body > 0 and
        c2_small and
        c3_bearish and c3_body > c2_body and
        c3_closes_into_c1
    )


def is_piercing_pattern(candles: List[Candle]) -> bool:
    """
    Piercing Pattern: Bearish candle followed by bullish candle 
    that opens below previous low and closes above midpoint.
    """
    if len(candles) < 2:
        return False
    
    prev, curr = candles[-2], candles[-1]
    
    prev_mid = (prev.open + prev.close) / 2
    
    return (
        is_bearish_candle(prev.open, prev.close) and
        is_bullish_candle(curr.open, curr.close) and
        curr.open < prev.low and
        curr.close > prev_mid
    )


def is_dark_cloud_cover(candles: List[Candle]) -> bool:
    """
    Dark Cloud Cover: Bullish candle followed by bearish candle
    that opens above previous high and closes below midpoint.
    """
    if len(candles) < 2:
        return False
    
    prev, curr = candles[-2], candles[-1]
    
    prev_mid = (prev.open + prev.close) / 2
    
    return (
        is_bullish_candle(prev.open, prev.close) and
        is_bearish_candle(curr.open, curr.close) and
        curr.open > prev.high and
        curr.close < prev_mid
    )


def is_harami_bullish(candles: List[Candle]) -> bool:
    """
    Bullish Harami: Large bearish candle followed by small bullish candle
    completely inside previous body.
    """
    if len(candles) < 2:
        return False
    
    prev, curr = candles[-2], candles[-1]
    
    prev_body_top = max(prev.open, prev.close)
    prev_body_bottom = min(prev.open, prev.close)
    
    return (
        is_bearish_candle(prev.open, prev.close) and
        is_bullish_candle(curr.open, curr.close) and
        curr.open > prev_body_bottom and
        curr.close < prev_body_top and
        candle_body(curr.open, curr.close) < candle_body(prev.open, prev.close) * 0.5
    )


def is_harami_bearish(candles: List[Candle]) -> bool:
    """
    Bearish Harami: Large bullish candle followed by small bearish candle
    completely inside previous body.
    """
    if len(candles) < 2:
        return False
    
    prev, curr = candles[-2], candles[-1]
    
    prev_body_top = max(prev.open, prev.close)
    prev_body_bottom = min(prev.open, prev.close)
    
    return (
        is_bullish_candle(prev.open, prev.close) and
        is_bearish_candle(curr.open, curr.close) and
        curr.close > prev_body_bottom and
        curr.open < prev_body_top and
        candle_body(curr.open, curr.close) < candle_body(prev.open, prev.close) * 0.5
    )


def is_three_white_soldiers(candles: List[Candle]) -> bool:
    """
    Three White Soldiers: Three consecutive bullish candles with 
    higher closes and small upper wicks.
    """
    if len(candles) < 3:
        return False
    
    c1, c2, c3 = candles[-3], candles[-2], candles[-1]
    
    def is_strong_bullish(c: Candle) -> bool:
        body = candle_body(c.open, c.close)
        range_ = candle_range(c.high, c.low)
        if range_ == 0:
            return False
        return (
            is_bullish_candle(c.open, c.close) and
            body / range_ > 0.6 and  # Large body
            upper_wick(c.high, c.open, c.close) / range_ < 0.2  # Small upper wick
        )
    
    return (
        is_strong_bullish(c1) and
        is_strong_bullish(c2) and
        is_strong_bullish(c3) and
        c2.close > c1.close and
        c3.close > c2.close
    )


def is_three_black_crows(candles: List[Candle]) -> bool:
    """
    Three Black Crows: Three consecutive bearish candles with
    lower closes and small lower wicks.
    """
    if len(candles) < 3:
        return False
    
    c1, c2, c3 = candles[-3], candles[-2], candles[-1]
    
    def is_strong_bearish(c: Candle) -> bool:
        body = candle_body(c.open, c.close)
        range_ = candle_range(c.high, c.low)
        if range_ == 0:
            return False
        return (
            is_bearish_candle(c.open, c.close) and
            body / range_ > 0.6 and
            lower_wick(c.low, c.open, c.close) / range_ < 0.2
        )
    
    return (
        is_strong_bearish(c1) and
        is_strong_bearish(c2) and
        is_strong_bearish(c3) and
        c2.close < c1.close and
        c3.close < c2.close
    )


# ── Master Detection Function ───────────────────────────────────

def detect_candlestick_patterns(candles: List[Candle]) -> List[str]:
    """
    Detect all candlestick patterns in recent candles.
    Returns list of pattern names found.
    """
    if len(candles) < 2:
        return []
    
    patterns_found = []
    
    # Single candle patterns
    if is_hammer(candles):
        patterns_found.append("hammer")
    
    if is_shooting_star(candles):
        patterns_found.append("shooting_star")
    
    # Two candle patterns
    if is_engulfing_bullish(candles):
        patterns_found.append("engulfing_bullish")
    
    if is_engulfing_bearish(candles):
        patterns_found.append("engulfing_bearish")
    
    if is_piercing_pattern(candles):
        patterns_found.append("piercing_pattern")
    
    if is_dark_cloud_cover(candles):
        patterns_found.append("dark_cloud_cover")
    
    if is_harami_bullish(candles):
        patterns_found.append("harami_bullish")
    
    if is_harami_bearish(candles):
        patterns_found.append("harami_bearish")
    
    # Three candle patterns
    if is_morning_star(candles):
        patterns_found.append("morning_star")
    
    if is_evening_star(candles):
        patterns_found.append("evening_star")
    
    if is_three_white_soldiers(candles):
        patterns_found.append("three_white_soldiers")
    
    if is_three_black_crows(candles):
        patterns_found.append("three_black_crows")
    
    return patterns_found


def get_pattern_details(candles: List[Candle]) -> List[PatternResult]:
    """
    Get detailed pattern information with strength and reliability.
    """
    if len(candles) < 2:
        return []
    
    results = []
    
    pattern_checks = [
        ("hammer", is_hammer, True, 8, "strong"),
        ("shooting_star", is_shooting_star, False, 8, "strong"),
        ("engulfing_bullish", is_engulfing_bullish, True, 9, "strong"),
        ("engulfing_bearish", is_engulfing_bearish, False, 9, "strong"),
        ("morning_star", is_morning_star, True, 9, "strong"),
        ("evening_star", is_evening_star, False, 9, "strong"),
        ("piercing_pattern", is_piercing_pattern, True, 7, "moderate"),
        ("dark_cloud_cover", is_dark_cloud_cover, False, 7, "moderate"),
        ("harami_bullish", is_harami_bullish, True, 6, "moderate"),
        ("harami_bearish", is_harami_bearish, False, 6, "moderate"),
        ("three_white_soldiers", is_three_white_soldiers, True, 8, "strong"),
        ("three_black_crows", is_three_black_crows, False, 8, "strong"),
    ]
    
    for name, check_fn, bullish, strength, reliability in pattern_checks:
        if check_fn(candles):
            desc = f"{'Bullish' if bullish else 'Bearish'} {name.replace('_', ' ').title()}"
            results.append(PatternResult(
                name=name,
                bullish=bullish,
                strength=strength,
                reliability=reliability,
                description=desc
            ))
    
    return results


def detect_support_resistance(candles: List[Candle], lookback: int = 20) -> Dict[str, Any]:
    """
    Detect support and resistance levels from recent price action.
    """
    if len(candles) < lookback:
        lookback = len(candles)
    
    recent = candles[-lookback:]
    highs = [c.high for c in recent]
    lows = [c.low for c in recent]
    
    # Simple pivot detection
    resistance = max(highs)
    support = min(lows)
    
    # Find multiple touches
    resistance_touches = sum(1 for h in highs if abs(h - resistance) / resistance < 0.001)
    support_touches = sum(1 for l in lows if abs(l - support) / support < 0.001)
    
    return {
        "resistance": resistance,
        "support": support,
        "resistance_touches": resistance_touches,
        "support_touches": support_touches,
        "range": resistance - support,
        "midpoint": (resistance + support) / 2
    }