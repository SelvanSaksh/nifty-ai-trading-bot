"""
Options chain analysis for Nifty 50.
Fetches PCR, Max Pain, and Open Interest shifts from Fyers or NSE.
"""
import aiohttp
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass

from config import settings

@dataclass
class OptionsContext:
    pcr: float                    # Put-Call Ratio
    max_pain_strike: float        # Max pain strike price
    max_pain_value: float         # Max pain value (total loss)
    call_oi_total: int
    put_oi_total: int
    call_oi_change: int           # Change in OI
    put_oi_change: int
    vix_regime: str               # low, normal, high, extreme
    nearest_expiry: str
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "pcr": round(self.pcr, 2),
            "max_pain_strike": self.max_pain_strike,
            "max_pain_value": round(self.max_pain_value, 2),
            "call_oi_total": self.call_oi_total,
            "put_oi_total": self.put_oi_total,
            "call_oi_change": self.call_oi_change,
            "put_oi_change": self.put_oi_change,
            "vix_regime": self.vix_regime,
            "nearest_expiry": self.nearest_expiry
        }


class OptionsAnalyzer:
    """Analyzes Nifty options chain for market context."""
    
    VIX_THRESHOLDS = {
        "low": 12,
        "normal": 20,
        "high": 25,
        "extreme": 30
    }
    
    def __init__(self):
        self._cache: Optional[OptionsContext] = None
        self._cache_time: Optional[datetime] = None
        self._cache_ttl = timedelta(minutes=5)
    
    async def get_context(self) -> OptionsContext:
        """Get options context (cached for 5 minutes)."""
        if self._cache and self._cache_time:
            if datetime.now() - self._cache_time < self._cache_ttl:
                return self._cache
        
        context = await self._fetch_options_data()
        self._cache = context
        self._cache_time = datetime.now()
        return context
    
    async def _fetch_options_data(self) -> OptionsContext:
        """
        Fetch options chain from Fyers API.
        In production, this calls Fyers option chain endpoint.
        """
        # Placeholder: In production, use Fyers option_chain API
        # fyers.option_chain(data={"symbol": "NSE:NIFTY50-INDEX", "strikecount": 10})
        
        # Simulated data for structure demonstration
        return OptionsContext(
            pcr=1.15,
            max_pain_strike=24500.0,
            max_pain_value=1250000.0,
            call_oi_total=4500000,
            put_oi_total=5200000,
            call_oi_change=125000,
            put_oi_change=180000,
            vix_regime=self._classify_vix(16.5),
            nearest_expiry=self._get_nearest_expiry()
        )
    
    def _classify_vix(self, vix_value: float) -> str:
        """Classify India VIX into regime."""
        if vix_value < self.VIX_THRESHOLDS["low"]:
            return "low"
        elif vix_value < self.VIX_THRESHOLDS["normal"]:
            return "normal"
        elif vix_value < self.VIX_THRESHOLDS["high"]:
            return "high"
        else:
            return "extreme"
    
    def _get_nearest_expiry(self) -> str:
        """Get nearest weekly/monthly expiry date."""
        today = datetime.now()
        # Nifty weekly expiry is every Thursday
        days_until_thursday = (3 - today.weekday()) % 7
        nearest = today + timedelta(days=days_until_thursday)
        return nearest.strftime("%Y-%m-%d")
    
    def get_size_multiplier(self, context: OptionsContext) -> float:
        """
        Reduce position size in extreme VIX conditions.
        Halved in extreme VIX as per documentation.
        """
        multipliers = {
            "low": 1.2,
            "normal": 1.0,
            "high": 0.75,
            "extreme": 0.5
        }
        return multipliers.get(context.vix_regime, 1.0)
    
    def get_bias(self, context: OptionsContext) -> str:
        """Determine market bias from PCR."""
        if context.pcr > 1.3:
            return "extreme_bearish"  # Too many puts = contrarian bullish
        elif context.pcr > 1.0:
            return "bearish"
        elif context.pcr > 0.7:
            return "neutral"
        elif context.pcr > 0.5:
            return "bullish"
        else:
            return "extreme_bullish"  # Too many calls = contrarian bearish


# Singleton instance
options_analyzer = OptionsAnalyzer()