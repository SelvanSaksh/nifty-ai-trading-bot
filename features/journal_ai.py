"""
AI-powered trade journaling with automatic tagging,
pattern recognition, and improvement suggestions.
"""
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime
import json

from models.trade import Trade
from models.signal import Signal


@dataclass
class JournalEntry:
    trade_id: str
    entry_time: datetime
    exit_time: Optional[datetime] = None
    direction: str = ""
    entry_price: float = 0
    exit_price: Optional[float] = None
    pnl: float = 0
    result: str = ""
    
    # AI-generated fields
    tags: List[str] = field(default_factory=list)
    mistakes: List[str] = field(default_factory=list)
    strengths: List[str] = field(default_factory=list)
    market_condition: str = ""      # trending, ranging, volatile, news-driven
    setup_quality: str = ""         # A, B, C, D
    emotion_score: int = 5          # 1-10
    notes: str = ""
    ai_summary: str = ""
    ai_suggestions: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "entry_time": self.entry_time.isoformat(),
            "exit_time": self.exit_time.isoformat() if self.exit_time else None,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "pnl": round(self.pnl, 2),
            "result": self.result,
            "tags": self.tags,
            "mistakes": self.mistakes,
            "strengths": self.strengths,
            "market_condition": self.market_condition,
            "setup_quality": self.setup_quality,
            "emotion_score": self.emotion_score,
            "notes": self.notes,
            "ai_summary": self.ai_summary,
            "ai_suggestions": self.ai_suggestions
        }


class TradeJournalAI:
    """
    AI-powered trade journal that automatically analyzes trades
    and provides insights for improvement.
    """
    
    def __init__(self):
        self.entries: Dict[str, JournalEntry] = {}
    
    def create_entry(self, trade: Trade, signal: Signal) -> JournalEntry:
        """Create journal entry from trade and signal data."""
        entry = JournalEntry(
            trade_id=trade.id,
            entry_time=trade.entry_time,
            direction=trade.direction.value,
            entry_price=trade.entry_price,
            pnl=trade.realized_pnl,
            result=trade.result.value if trade.result else "PENDING"
        )
        
        # Auto-tag based on trade characteristics
        entry.tags = self._auto_tag(trade, signal)
        entry.market_condition = self._classify_market_condition(trade, signal)
        entry.setup_quality = self._grade_setup(trade, signal)
        
        # Generate AI summary
        entry.ai_summary = self._generate_summary(entry, trade, signal)
        entry.ai_suggestions = self._generate_suggestions(entry, trade, signal)
        
        self.entries[trade.id] = entry
        return entry
    
    def update_entry(self, trade_id: str, **kwargs) -> bool:
        """Update journal entry with user notes or corrections."""
        if trade_id not in self.entries:
            return False
        
        entry = self.entries[trade_id]
        for key, value in kwargs.items():
            if hasattr(entry, key):
                setattr(entry, key, value)
        
        return True
    
    def get_entry(self, trade_id: str) -> Optional[JournalEntry]:
        return self.entries.get(trade_id)
    
    def get_all_entries(self) -> List[JournalEntry]:
        return sorted(self.entries.values(), key=lambda x: x.entry_time, reverse=True)
    
    def get_entries_by_tag(self, tag: str) -> List[JournalEntry]:
        return [e for e in self.entries.values() if tag in e.tags]
    
    def get_pattern_analysis(self) -> Dict[str, Any]:
        """Analyze patterns across all journal entries."""
        entries = list(self.entries.values())
        if not entries:
            return {}
        
        total = len(entries)
        wins = [e for e in entries if e.result == "WIN"]
        losses = [e for e in entries if e.result == "LOSS"]
        
        # Analyze by setup quality
        quality_performance = {}
        for q in ["A", "B", "C", "D"]:
            q_entries = [e for e in entries if e.setup_quality == q]
            q_wins = [e for e in q_entries if e.result == "WIN"]
            quality_performance[q] = {
                "total": len(q_entries),
                "wins": len(q_wins),
                "win_rate": round(len(q_wins) / len(q_entries) * 100, 2) if q_entries else 0
            }
        
        # Analyze by market condition
        condition_performance = {}
        for condition in ["trending", "ranging", "volatile", "news-driven"]:
            c_entries = [e for e in entries if e.market_condition == condition]
            c_wins = [e for e in c_entries if e.result == "WIN"]
            condition_performance[condition] = {
                "total": len(c_entries),
                "wins": len(c_wins),
                "win_rate": round(len(c_wins) / len(c_entries) * 100, 2) if c_entries else 0
            }
        
        # Most common mistakes
        all_mistakes = []
        for e in entries:
            all_mistakes.extend(e.mistakes)
        mistake_counts = {}
        for m in all_mistakes:
            mistake_counts[m] = mistake_counts.get(m, 0) + 1
        
        # Most common strengths
        all_strengths = []
        for e in entries:
            all_strengths.extend(e.strengths)
        strength_counts = {}
        for s in all_strengths:
            strength_counts[s] = strength_counts.get(s, 0) + 1
        
        return {
            "total_entries": total,
            "win_rate": round(len(wins) / total * 100, 2) if total > 0 else 0,
            "avg_pnl": round(sum(e.pnl for e in entries) / total, 2) if total > 0 else 0,
            "by_setup_quality": quality_performance,
            "by_market_condition": condition_performance,
            "common_mistakes": dict(sorted(mistake_counts.items(), key=lambda x: x[1], reverse=True)[:5]),
            "common_strengths": dict(sorted(strength_counts.items(), key=lambda x: x[1], reverse=True)[:5])
        }
    
    def _auto_tag(self, trade: Trade, signal: Signal) -> List[str]:
        """Automatically generate tags based on trade characteristics."""
        tags = []
        
        # Direction tag
        tags.append(trade.direction.value.lower())
        
        # Score-based tags
        if signal.score >= 80:
            tags.append("high_confidence")
        elif signal.score >= 60:
            tags.append("medium_confidence")
        else:
            tags.append("low_confidence")
        
        # P&L tags
        if trade.realized_pnl > 0:
            tags.append("winner")
            if trade.realized_pnl > 5000:
                tags.append("big_winner")
        else:
            tags.append("loser")
            if trade.realized_pnl < -3000:
                tags.append("big_loser")
        
        # Risk-reward tag
        if signal.risk_reward >= 2:
            tags.append("high_rr")
        
        # Time-based tags
        hour = trade.entry_time.hour
        if 9 <= hour <= 11:
            tags.append("opening_range")
        elif 12 <= hour <= 14:
            tags.append("mid_session")
        else:
            tags.append("closing_hour")
        
        return tags
    
    def _classify_market_condition(self, trade: Trade, signal: Signal) -> str:
        """Classify market condition based on signal and trade data."""
        # Simplified classification
        if signal.score >= 75 and abs(trade.realized_pnl) > 2000:
            return "trending"
        elif signal.score < 60:
            return "ranging"
        elif abs(trade.realized_pnl) > 5000:
            return "volatile"
        else:
            return "normal"
    
    def _grade_setup(self, trade: Trade, signal: Signal) -> str:
        """Grade the trade setup quality (A-D)."""
        score = signal.score
        rr = signal.risk_reward
        
        if score >= 80 and rr >= 2.0:
            return "A"
        elif score >= 65 and rr >= 1.5:
            return "B"
        elif score >= 55 and rr >= 1.2:
            return "C"
        else:
            return "D"
    
    def _generate_summary(self, entry: JournalEntry, trade: Trade, signal: Signal) -> str:
        """Generate AI summary of the trade."""
        result_emoji = "✅" if trade.result and trade.result.value == "WIN" else "❌"
        
        return (
            f"{result_emoji} {trade.direction.value} trade on Nifty 50. "
            f"Entry at {trade.entry_price}, exited at {entry.exit_price or 'N/A'}. "
            f"P&L: ₹{trade.realized_pnl:,.2f}. "
            f"Signal score: {signal.score}/100 with R:R of {signal.risk_reward:.2f}. "
            f"Setup graded {entry.setup_quality}."
        )
    
    def _generate_suggestions(self, entry: JournalEntry, trade: Trade, signal: Signal) -> List[str]:
        """Generate improvement suggestions based on trade analysis."""
        suggestions = []
        
        if trade.result and trade.result.value == "LOSS":
            if signal.score < 65:
                suggestions.append("Consider waiting for higher-confidence setups (score > 70)")
            if signal.risk_reward < 1.5:
                suggestions.append("Look for setups with better risk-reward ratio (min 1.5:1)")
            
            # Check if stop was too tight
            risk = abs(trade.entry_price - trade.stop_loss)
            if risk < 30:  # Very tight stop for Nifty
                suggestions.append("Stop loss may have been too tight — consider wider stops in volatile conditions")
        
        if entry.setup_quality == "D":
            suggestions.append("This was a low-quality setup — review entry criteria")
        
        if trade.direction.value == "SHORT" and signal.score > 70:
            suggestions.append("Strong short setup — consider increasing position size next time")
        
        # Add default suggestions if list is empty
        if not suggestions:
            suggestions.append("Good trade execution — maintain this approach")
        
        return suggestions


# Singleton
trade_journal = TradeJournalAI()