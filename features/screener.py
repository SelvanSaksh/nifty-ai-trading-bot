"""
Real-time market screener for finding trade setups across instruments.
"""
from typing import List, Dict, Optional, Any, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import asyncio

from models.enums import Direction


class ScreenerFilter(Enum):
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    RSI_OVERBOUGHT = "rsi_overbought"
    RSI_OVERSOLD = "rsi_oversold"
    VOLUME_SPIKE = "volume_spike"
    EMA_CROSSOVER_BULLISH = "ema_crossover_bullish"
    EMA_CROSSOVER_BEARISH = "ema_crossover_bearish"
    BB_SQUEEZE = "bb_squeeze"
    ABOVE_VWAP = "above_vwap"
    BELOW_VWAP = "below_vwap"


@dataclass
class ScreenerResult:
    symbol: str
    name: str
    price: float
    change_pct: float
    volume: int
    filters_matched: List[ScreenerFilter]
    signal_direction: Optional[Direction] = None
    signal_score: int = 0
    timestamp: datetime = datetime.now()
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "price": self.price,
            "change_pct": round(self.change_pct, 2),
            "volume": self.volume,
            "filters_matched": [f.value for f in self.filters_matched],
            "signal_direction": self.signal_direction.value if self.signal_direction else None,
            "signal_score": self.signal_score,
            "timestamp": self.timestamp.isoformat()
        }


class MarketScreener:
    """
    Screens multiple instruments for trading opportunities.
    In production, fetches live data for all symbols.
    """
    
    def __init__(self):
        self.symbols = [
            "NSE:NIFTY50-INDEX",
            "NSE:NIFTYBANK-INDEX",
            "NSE:FINNIFTY-INDEX",
            "NSE:SENSEX-INDEX",
            "NSE:RELIANCE-EQ",
            "NSE:TCS-EQ",
            "NSE:INFY-EQ",
            "NSE:HDFCBANK-EQ",
            "NSE:ICICIBANK-EQ",
            "NSE:SBIN-EQ",
            "NSE:BAJFINANCE-EQ",
            "NSE:KOTAKBANK-EQ",
            "NSE:ITC-EQ",
            "NSE:HINDUNILVR-EQ",
            "NSE:LT-EQ",
        ]
        self._filters: Dict[ScreenerFilter, Callable] = {
            ScreenerFilter.TRENDING_UP: self._filter_trending_up,
            ScreenerFilter.TRENDING_DOWN: self._filter_trending_down,
            ScreenerFilter.RSI_OVERBOUGHT: self._filter_rsi_overbought,
            ScreenerFilter.RSI_OVERSOLD: self._filter_rsi_oversold,
            ScreenerFilter.VOLUME_SPIKE: self._filter_volume_spike,
            ScreenerFilter.EMA_CROSSOVER_BULLISH: self._filter_ema_bullish,
            ScreenerFilter.EMA_CROSSOVER_BEARISH: self._filter_ema_bearish,
            ScreenerFilter.BB_SQUEEZE: self._filter_bb_squeeze,
            ScreenerFilter.ABOVE_VWAP: self._filter_above_vwap,
            ScreenerFilter.BELOW_VWAP: self._filter_below_vwap,
        }
    
    async def screen(self, active_filters: List[ScreenerFilter]) -> List[ScreenerResult]:
        """
        Run screening with selected filters.
        Returns instruments matching ALL selected filters.
        """
        results = []
        
        # In production: fetch live data for all symbols in parallel
        for symbol in self.symbols:
            data = await self._fetch_symbol_data(symbol)
            if not data:
                continue
            
            matched = []
            for filt in active_filters:
                evaluator = self._filters.get(filt)
                if evaluator and evaluator(data):
                    matched.append(filt)
            
            # Match if ALL filters matched
            if len(matched) == len(active_filters):
                # Determine signal direction
                direction = self._infer_direction(matched)
                score = self._calculate_score(matched, data)
                
                results.append(ScreenerResult(
                    symbol=symbol,
                    name=data.get("name", symbol),
                    price=data.get("price", 0),
                    change_pct=data.get("change_pct", 0),
                    volume=data.get("volume", 0),
                    filters_matched=matched,
                    signal_direction=direction,
                    signal_score=score
                ))
        
        # Sort by signal score descending
        results.sort(key=lambda x: x.signal_score, reverse=True)
        return results
    
    async def _fetch_symbol_data(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Fetch live data for a symbol.
        In production: call Fyers quote API.
        """
        # Placeholder: return simulated data
        import random
        return {
            "symbol": symbol,
            "name": symbol.split(":")[1].replace("-INDEX", "").replace("-EQ", ""),
            "price": 24000 + random.randint(-500, 500),
            "change_pct": random.uniform(-2, 2),
            "volume": random.randint(100000, 10000000),
            "rsi": random.uniform(20, 80),
            "ema_20": 23900,
            "ema_50": 23800,
            "ema_200": 23500,
            "vwap": 24050,
            "bb_width": random.uniform(0.5, 3),
            "avg_volume": 5000000,
        }
    
    def _infer_direction(self, matched: List[ScreenerFilter]) -> Optional[Direction]:
        """Infer trade direction from matched filters."""
        bullish = [ScreenerFilter.TRENDING_UP, ScreenerFilter.RSI_OVERSOLD,
                   ScreenerFilter.EMA_CROSSOVER_BULLISH, ScreenerFilter.ABOVE_VWAP]
        bearish = [ScreenerFilter.TRENDING_DOWN, ScreenerFilter.RSI_OVERBOUGHT,
                   ScreenerFilter.EMA_CROSSOVER_BEARISH, ScreenerFilter.BELOW_VWAP]
        
        bull_count = sum(1 for f in matched if f in bullish)
        bear_count = sum(1 for f in matched if f in bearish)
        
        if bull_count > bear_count:
            return Direction.LONG
        elif bear_count > bull_count:
            return Direction.SHORT
        return None
    
    def _calculate_score(self, matched: List[ScreenerFilter], data: Dict) -> int:
        """Calculate opportunity score based on filters and data quality."""
        base_score = len(matched) * 10
        
        # Bonus for strong trends
        if ScreenerFilter.TRENDING_UP in matched or ScreenerFilter.TRENDING_DOWN in matched:
            base_score += 15
        
        # Bonus for volume confirmation
        if ScreenerFilter.VOLUME_SPIKE in matched:
            base_score += 10
        
        # Cap at 100
        return min(100, base_score)
    
    # ── Filter evaluators ───────────────────────────────────────
    def _filter_trending_up(self, data: Dict) -> bool:
        return data.get("ema_20", 0) > data.get("ema_50", 0) > data.get("ema_200", 0)
    
    def _filter_trending_down(self, data: Dict) -> bool:
        return data.get("ema_20", float('inf')) < data.get("ema_50", float('inf')) < data.get("ema_200", float('inf'))
    
    def _filter_rsi_overbought(self, data: Dict) -> bool:
        return data.get("rsi", 0) > 70
    
    def _filter_rsi_oversold(self, data: Dict) -> bool:
        return data.get("rsi", 100) < 30
    
    def _filter_volume_spike(self, data: Dict) -> bool:
        return data.get("volume", 0) > data.get("avg_volume", 1) * 1.5
    
    def _filter_ema_bullish(self, data: Dict) -> bool:
        return data.get("ema_20", 0) > data.get("ema_50", 0)
    
    def _filter_ema_bearish(self, data: Dict) -> bool:
        return data.get("ema_20", float('inf')) < data.get("ema_50", float('inf'))
    
    def _filter_bb_squeeze(self, data: Dict) -> bool:
        return data.get("bb_width", 10) < 1.5  # Tight Bollinger Bands
    
    def _filter_above_vwap(self, data: Dict) -> bool:
        return data.get("price", 0) > data.get("vwap", float('inf'))
    
    def _filter_below_vwap(self, data: Dict) -> bool:
        return data.get("price", float('inf')) < data.get("vwap", 0)


# Singleton
market_screener = MarketScreener()