"""
Portfolio tracking and performance analytics.
"""
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from enum import Enum
import json

from models.trade import Trade
from models.enums import SignalResult


class Timeframe(Enum):
    TODAY = "today"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"
    ALL = "all"


@dataclass
class PortfolioSnapshot:
    timestamp: datetime
    total_value: float
    cash: float
    invested: float
    unrealized_pnl: float
    realized_pnl: float
    total_return_pct: float
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "total_value": round(self.total_value, 2),
            "cash": round(self.cash, 2),
            "invested": round(self.invested, 2),
            "unrealized_pnl": round(self.unrealized_pnl, 2),
            "realized_pnl": round(self.realized_pnl, 2),
            "total_return_pct": round(self.total_return_pct, 2)
        }


@dataclass
class PerformanceMetrics:
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    max_drawdown: float
    sharpe_ratio: float
    calmar_ratio: float
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate": round(self.win_rate, 2),
            "avg_win": round(self.avg_win, 2),
            "avg_loss": round(self.avg_loss, 2),
            "profit_factor": round(self.profit_factor, 2),
            "max_drawdown": round(self.max_drawdown, 2),
            "sharpe_ratio": round(self.sharpe_ratio, 2),
            "calmar_ratio": round(self.calmar_ratio, 2)
        }


class PortfolioTracker:
    """Tracks portfolio value and computes performance metrics."""
    
    def __init__(self, initial_capital: float = 100000):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.invested = 0.0
        self.realized_pnl = 0.0
        self.trades: List[Trade] = []
        self.snapshots: List[PortfolioSnapshot] = []
        self.equity_curve: List[float] = [initial_capital]
        self.daily_returns: List[float] = []
    
    def add_trade(self, trade: Trade):
        """Record a completed trade."""
        self.trades.append(trade)
        self.realized_pnl += trade.realized_pnl
        self.cash += trade.realized_pnl  # Simplified: assumes full cash settlement
        
        # Update equity curve
        current_equity = self.initial_capital + self.realized_pnl
        self.equity_curve.append(current_equity)
        
        # Calculate daily return
        if len(self.equity_curve) > 1:
            daily_return = (self.equity_curve[-1] - self.equity_curve[-2]) / self.equity_curve[-2]
            self.daily_returns.append(daily_return)
    
    def get_current_value(self) -> float:
        """Current portfolio value (cash + unrealized)."""
        return self.cash + self.invested + self._unrealized_pnl()
    
    def _unrealized_pnl(self) -> float:
        """Calculate unrealized P&L from open positions."""
        # In production: fetch current market prices for open positions
        return 0.0
    
    def take_snapshot(self) -> PortfolioSnapshot:
        """Record current portfolio state."""
        total = self.get_current_value()
        return_pct = ((total - self.initial_capital) / self.initial_capital) * 100
        
        snapshot = PortfolioSnapshot(
            timestamp=datetime.now(),
            total_value=total,
            cash=self.cash,
            invested=self.invested,
            unrealized_pnl=self._unrealized_pnl(),
            realized_pnl=self.realized_pnl,
            total_return_pct=return_pct
        )
        self.snapshots.append(snapshot)
        return snapshot
    
    def get_metrics(self, timeframe: Timeframe = Timeframe.ALL) -> PerformanceMetrics:
        """Calculate performance metrics for given timeframe."""
        filtered_trades = self._filter_trades_by_timeframe(timeframe)
        
        if not filtered_trades:
            return PerformanceMetrics(0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        
        wins = [t for t in filtered_trades if t.result and t.result.value == "WIN"]
        losses = [t for t in filtered_trades if t.result and t.result.value == "LOSS"]
        
        total_trades = len(filtered_trades)
        winning_trades = len(wins)
        losing_trades = len(losses)
        win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0
        
        avg_win = sum(t.realized_pnl for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t.realized_pnl for t in losses) / len(losses) if losses else 0
        
        gross_profit = sum(t.realized_pnl for t in wins)
        gross_loss = abs(sum(t.realized_pnl for t in losses))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
        
        max_dd = self._calculate_max_drawdown()
        sharpe = self._calculate_sharpe()
        calmar = self._calculate_calmar(max_dd)
        
        return PerformanceMetrics(
            total_trades=total_trades,
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            win_rate=win_rate,
            avg_win=avg_win,
            avg_loss=avg_loss,
            profit_factor=profit_factor,
            max_drawdown=max_dd,
            sharpe_ratio=sharpe,
            calmar_ratio=calmar
        )
    
    def _filter_trades_by_timeframe(self, timeframe: Timeframe) -> List[Trade]:
        """Filter trades by timeframe."""
        now = datetime.now()
        
        if timeframe == Timeframe.TODAY:
            cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif timeframe == Timeframe.WEEK:
            cutoff = now - timedelta(days=now.weekday())
            cutoff = cutoff.replace(hour=0, minute=0, second=0, microsecond=0)
        elif timeframe == Timeframe.MONTH:
            cutoff = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        elif timeframe == Timeframe.YEAR:
            cutoff = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            return self.trades
        
        return [t for t in self.trades if t.entry_time >= cutoff]
    
    def _calculate_max_drawdown(self) -> float:
        """Calculate maximum drawdown from equity curve."""
        if len(self.equity_curve) < 2:
            return 0.0
        
        peak = self.equity_curve[0]
        max_dd = 0.0
        
        for value in self.equity_curve:
            if value > peak:
                peak = value
            dd = (peak - value) / peak
            max_dd = max(max_dd, dd)
        
        return max_dd * 100  # As percentage
    
    def _calculate_sharpe(self, risk_free_rate: float = 0.06) -> float:
        """Calculate annualized Sharpe ratio."""
        if len(self.daily_returns) < 2:
            return 0.0
        
        import numpy as np
        returns = np.array(self.daily_returns)
        excess_returns = returns - (risk_free_rate / 252)  # Daily risk-free rate
        return (np.mean(excess_returns) / np.std(excess_returns)) * np.sqrt(252) if np.std(excess_returns) > 0 else 0
    
    def _calculate_calmar(self, max_dd: float) -> float:
        """Calculate Calmar ratio."""
        if max_dd <= 0:
            return 0.0
        
        total_return = ((self.get_current_value() - self.initial_capital) / self.initial_capital) * 100
        # Annualized return approximation
        years = max(1, len(self.trades) / 250)  # Approximate trading days
        annualized_return = total_return / years
        
        return annualized_return / max_dd
    
    def get_equity_curve(self) -> List[Dict[str, Any]]:
        """Get equity curve data for charting."""
        return [
            {"day": i, "equity": round(eq, 2)}
            for i, eq in enumerate(self.equity_curve)
        ]
    
    def get_trade_distribution(self) -> Dict[str, Any]:
        """Get trade distribution by hour, day, direction."""
        if not self.trades:
            return {}
        
        by_hour = {}
        by_day = {}
        by_direction = {"LONG": 0, "SHORT": 0}
        
        for trade in self.trades:
            hour = trade.entry_time.hour
            day = trade.entry_time.strftime("%A")
            by_hour[hour] = by_hour.get(hour, 0) + 1
            by_day[day] = by_day.get(day, 0) + 1
            by_direction[trade.direction.value] = by_direction.get(trade.direction.value, 0) + 1
        
        return {
            "by_hour": by_hour,
            "by_day": by_day,
            "by_direction": by_direction
        }


# Singleton
portfolio_tracker = PortfolioTracker()