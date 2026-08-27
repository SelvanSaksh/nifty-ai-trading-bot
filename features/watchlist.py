"""
User watchlists for tracking multiple instruments.
"""
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class WatchlistItem:
    symbol: str                    # e.g., "NSE:NIFTY50-INDEX"
    name: str                      # Display name
    exchange: str = "NSE"
    instrument_type: str = "INDEX"
    added_at: datetime = field(default_factory=datetime.now)
    notes: str = ""
    alerts_enabled: bool = True
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "exchange": self.exchange,
            "instrument_type": self.instrument_type,
            "added_at": self.added_at.isoformat(),
            "notes": self.notes,
            "alerts_enabled": self.alerts_enabled
        }


class Watchlist:
    """A named collection of watchlist items."""
    
    def __init__(self, id: str, name: str, user_id: str = "default"):
        self.id = id
        self.name = name
        self.user_id = user_id
        self.items: List[WatchlistItem] = []
        self.created_at = datetime.now()
        self.updated_at = datetime.now()
    
    def add(self, item: WatchlistItem) -> bool:
        """Add item if not already present."""
        if not any(i.symbol == item.symbol for i in self.items):
            self.items.append(item)
            self.updated_at = datetime.now()
            return True
        return False
    
    def remove(self, symbol: str) -> bool:
        """Remove item by symbol."""
        for i, item in enumerate(self.items):
            if item.symbol == symbol:
                self.items.pop(i)
                self.updated_at = datetime.now()
                return True
        return False
    
    def get(self, symbol: str) -> Optional[WatchlistItem]:
        """Get item by symbol."""
        for item in self.items:
            if item.symbol == symbol:
                return item
        return None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "user_id": self.user_id,
            "item_count": len(self.items),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "items": [i.to_dict() for i in self.items]
        }


class WatchlistManager:
    """Manages multiple watchlists per user."""
    
    def __init__(self):
        self._watchlists: Dict[str, Watchlist] = {}
        self._default_symbols = [
            ("NSE:NIFTY50-INDEX", "Nifty 50", "INDEX", "NSE"),
            ("NSE:NIFTYBANK-INDEX", "Bank Nifty", "INDEX", "NSE"),
            ("NSE:FINNIFTY-INDEX", "Fin Nifty", "INDEX", "NSE"),
            ("NSE:SENSEX-INDEX", "Sensex", "INDEX", "NSE"),
            ("NSE:RELIANCE-EQ", "Reliance", "EQUITY", "NSE"),
            ("NSE:TCS-EQ", "TCS", "EQUITY", "NSE"),
            ("NSE:INFY-EQ", "Infosys", "EQUITY", "NSE"),
            ("NSE:HDFCBANK-EQ", "HDFC Bank", "EQUITY", "NSE"),
            ("NSE:ICICIBANK-EQ", "ICICI Bank", "EQUITY", "NSE"),
            ("NSE:SBIN-EQ", "SBI", "EQUITY", "NSE"),
            ("NSE:BAJFINANCE-EQ", "Bajaj Finance", "EQUITY", "NSE"),
            ("NSE:KOTAKBANK-EQ", "Kotak Bank", "EQUITY", "NSE"),
            ("NSE:ITC-EQ", "ITC", "EQUITY", "NSE"),
            ("NSE:HINDUNILVR-EQ", "Hindustan Unilever", "EQUITY", "NSE"),
            ("NSE:LT-EQ", "L&T", "EQUITY", "NSE"),
        ]
    
    def create_default(self, user_id: str = "default") -> Watchlist:
        """Create default watchlist with common symbols."""
        wl = Watchlist(
            id=f"wl_{user_id}_default",
            name="My Watchlist",
            user_id=user_id
        )
        for symbol, name, instrument_type, exchange in self._default_symbols:
            wl.add(WatchlistItem(
                symbol=symbol,
                name=name,
                instrument_type=instrument_type,
                exchange=exchange,
            ))
        
        self._watchlists[wl.id] = wl
        return wl
    
    def create(self, user_id: str, name: str) -> Watchlist:
        """Create new empty watchlist."""
        wl_id = f"wl_{user_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        wl = Watchlist(id=wl_id, name=name, user_id=user_id)
        self._watchlists[wl_id] = wl
        return wl
    
    def get(self, watchlist_id: str) -> Optional[Watchlist]:
        return self._watchlists.get(watchlist_id)
    
    def get_user_watchlists(self, user_id: str) -> List[Watchlist]:
        return [wl for wl in self._watchlists.values() if wl.user_id == user_id]
    
    def delete(self, watchlist_id: str) -> bool:
        if watchlist_id in self._watchlists:
            del self._watchlists[watchlist_id]
            return True
        return False


# Singleton
watchlist_manager = WatchlistManager()