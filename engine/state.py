from dataclasses import dataclass
from datetime import datetime, timezone

@dataclass
class Position:
    symbol: str
    side: str            # "long" / "short"
    entry_price: float
    size: float
    opened_at: str
    stop_price: float
    take_profit_price: float | None

@dataclass
class EngineState:
    position: Position | None
    trades_today: int
    day_utc: str
    equity: float
    daily_pnl: float
    day_start_equity: float

def utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def new_state(initial_equity: float) -> EngineState:
    return EngineState(
        position=None,
        trades_today=0,
        day_utc=utc_day(),
        equity=initial_equity,
        daily_pnl=0.0,
        day_start_equity=initial_equity
    )
