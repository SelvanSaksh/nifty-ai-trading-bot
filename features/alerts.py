"""
Configurable alert system for price levels, indicator thresholds,
and custom conditions.
"""
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Callable, Any
from dataclasses import dataclass, field
from enum import Enum
import asyncio

from notifier import TelegramNotifier


class AlertType(Enum):
    PRICE_ABOVE = "price_above"
    PRICE_BELOW = "price_below"
    RSI_OVERBOUGHT = "rsi_overbought"
    RSI_OVERSOLD = "rsi_oversold"
    EMA_CROSSOVER = "ema_crossover"
    VOLUME_SPIKE = "volume_spike"
    CUSTOM = "custom"


class AlertStatus(Enum):
    ACTIVE = "active"
    TRIGGERED = "triggered"
    DISABLED = "disabled"


@dataclass
class Alert:
    id: str
    name: str
    alert_type: AlertType
    condition: Dict[str, Any]      # e.g., {"price": 25000, "symbol": "NIFTY"}
    status: AlertStatus = AlertStatus.ACTIVE
    created_at: datetime = field(default_factory=datetime.now)
    triggered_at: Optional[datetime] = None
    trigger_count: int = 0
    max_triggers: int = 1          # 0 = unlimited
    cooldown_minutes: int = 30
    last_triggered: Optional[datetime] = None
    message_template: str = ""
    
    def can_trigger(self) -> bool:
        if self.status != AlertStatus.ACTIVE:
            return False
        if self.max_triggers > 0 and self.trigger_count >= self.max_triggers:
            return False
        if self.last_triggered:
            cooldown_end = self.last_triggered + timedelta(minutes=self.cooldown_minutes)
            if datetime.now() < cooldown_end:
                return False
        return True


class AlertManager:
    """Manages and evaluates trading alerts."""
    
    def __init__(self):
        self.alerts: List[Alert] = []
        self.notifier = TelegramNotifier()
        self._evaluators: Dict[AlertType, Callable] = {
            AlertType.PRICE_ABOVE: self._eval_price_above,
            AlertType.PRICE_BELOW: self._eval_price_below,
            AlertType.RSI_OVERBOUGHT: self._eval_rsi_overbought,
            AlertType.RSI_OVERSOLD: self._eval_rsi_oversold,
            AlertType.EMA_CROSSOVER: self._eval_ema_crossover,
            AlertType.VOLUME_SPIKE: self._eval_volume_spike,
            AlertType.CUSTOM: self._eval_custom,
        }
    
    def add_alert(self, alert: Alert) -> str:
        """Add a new alert."""
        self.alerts.append(alert)
        return alert.id
    
    def remove_alert(self, alert_id: str) -> bool:
        """Remove alert by ID."""
        for i, alert in enumerate(self.alerts):
            if alert.id == alert_id:
                self.alerts.pop(i)
                return True
        return False
    
    def get_alerts(self, status: Optional[AlertStatus] = None) -> List[Alert]:
        """Get alerts, optionally filtered by status."""
        if status:
            return [a for a in self.alerts if a.status == status]
        return self.alerts
    
    async def evaluate(self, market_data: Dict[str, Any]):
        """Evaluate all active alerts against current market data."""
        for alert in self.alerts:
            if not alert.can_trigger():
                continue
            
            evaluator = self._evaluators.get(alert.alert_type)
            if evaluator and evaluator(alert, market_data):
                await self._trigger_alert(alert, market_data)
    
    async def _trigger_alert(self, alert: Alert, market_data: Dict[str, Any]):
        """Trigger alert and send notification."""
        alert.trigger_count += 1
        alert.triggered_at = datetime.now()
        alert.last_triggered = datetime.now()
        
        if alert.max_triggers > 0 and alert.trigger_count >= alert.max_triggers:
            alert.status = AlertStatus.TRIGGERED
        
        # Build message
        message = alert.message_template or self._build_default_message(alert, market_data)
        
        await self.notifier.send(f"🔔 ALERT: {alert.name}\n{message}")
    
    def _build_default_message(self, alert: Alert, market_data: Dict) -> str:
        return (
            f"Type: {alert.alert_type.value}\n"
            f"Condition: {alert.condition}\n"
            f"Current Price: {market_data.get('price', 'N/A')}\n"
            f"Time: {datetime.now().strftime('%H:%M:%S')}"
        )
    
    # ── Evaluators ──────────────────────────────────────────────
    def _eval_price_above(self, alert: Alert, data: Dict) -> bool:
        return data.get("price", 0) > alert.condition.get("price", float('inf'))
    
    def _eval_price_below(self, alert: Alert, data: Dict) -> bool:
        return data.get("price", float('inf')) < alert.condition.get("price", 0)
    
    def _eval_rsi_overbought(self, alert: Alert, data: Dict) -> bool:
        return data.get("rsi", 0) > alert.condition.get("threshold", 70)
    
    def _eval_rsi_oversold(self, alert: Alert, data: Dict) -> bool:
        return data.get("rsi", 100) < alert.condition.get("threshold", 30)
    
    def _eval_ema_crossover(self, alert: Alert, data: Dict) -> bool:
        ema_fast = data.get("ema_fast")
        ema_slow = data.get("ema_slow")
        prev_fast = data.get("prev_ema_fast")
        prev_slow = data.get("prev_ema_slow")
        
        if all(v is not None for v in [ema_fast, ema_slow, prev_fast, prev_slow]):
            cross_up = prev_fast <= prev_slow and ema_fast > ema_slow
            cross_down = prev_fast >= prev_slow and ema_fast < ema_slow
            direction = alert.condition.get("direction", "up")
            return cross_up if direction == "up" else cross_down
        return False
    
    def _eval_volume_spike(self, alert: Alert, data: Dict) -> bool:
        current_vol = data.get("volume", 0)
        avg_vol = data.get("avg_volume", 1)
        threshold = alert.condition.get("multiplier", 2.0)
        return current_vol > avg_vol * threshold
    
    def _eval_custom(self, alert: Alert, data: Dict) -> bool:
        """Custom evaluator - uses a lambda stored in condition."""
        custom_fn = alert.condition.get("evaluator")
        if callable(custom_fn):
            return custom_fn(data)
        return False


# Singleton
alert_manager = AlertManager()