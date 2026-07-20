from enum import Enum

class Direction(Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    WAIT = "WAIT"

class TradeStatus(Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    PARTIAL_1 = "PARTIAL_1"   
    PARTIAL_2 = "PARTIAL_2"  
    CLOSED = "CLOSED"

class SignalResult(Enum):
    WIN = "WIN"
    LOSS = "LOSS"