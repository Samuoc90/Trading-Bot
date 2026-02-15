from __future__ import annotations

from dataclasses import dataclass, field
from collections import deque
from typing import Deque, Optional, Tuple, Dict, Any


def _ema_update(prev: Optional[float], price: float, length: int) -> float:
    if prev is None:
        return price
    alpha = 2.0 / (length + 1.0)
    return prev + alpha * (price - prev)


@dataclass
class TrendPullbackState:
    ema20: Optional[float] = None
    ema50: Optional[float] = None
    ema200: Optional[float] = None
    lows: Deque[float] = field(default_factory=lambda: deque(maxlen=50))
    highs: Deque[float] = field(default_factory=lambda: deque(maxlen=50))


def on_candle(
    state: TrendPullbackState,
    candle: Dict[str, Any],
    ema_trend: int = 200,
    ema_fast: int = 20,
    ema_slow: int = 50,
    pullback_band_pct: float = 0.0015,
    swing_lookback: int = 5
) -> Tuple[TrendPullbackState, Optional[str], Dict[str, float]]:
    """
    Returns: (updated_state, signal 'long'/'short'/None, info dict)
    Candle expects: open, high, low, close (floats)
    """
    o = float(candle["open"])
    h = float(candle["high"])
    l = float(candle["low"])
    c = float(candle["close"])

    # Update rolling highs/lows
    state.lows.append(l)
    state.highs.append(h)

    # Update EMAs on close
    state.ema20 = _ema_update(state.ema20, c, ema_fast)
    state.ema50 = _ema_update(state.ema50, c, ema_slow)
    state.ema200 = _ema_update(state.ema200, c, ema_trend)

    # Need all EMAs initialized
    if state.ema20 is None or state.ema50 is None or state.ema200 is None:
        return state, None, {}

    # Trend filter
    trend_up = c > state.ema200
    trend_down = c < state.ema200

    # Pullback touch (ODER): wir prüfen "Touch" über low/high Nähe
    def near(x: float, ema: float) -> bool:
        return abs(x - ema) / ema <= pullback_band_pct

    touch_fast_up = near(l, state.ema20)
    touch_slow_up = near(l, state.ema50)
    touch_fast_dn = near(h, state.ema20)
    touch_slow_dn = near(h, state.ema50)

    # Candle direction (simpel, robust)
    bullish = c > o
    bearish = c < o

    # Swing levels für Stop
    # (Wir nehmen min low / max high der letzten N Candles)
    look = max(1, min(swing_lookback, len(state.lows)))
    recent_lows = list(state.lows)[-look:]
    recent_highs = list(state.highs)[-look:]
    swing_low = min(recent_lows) if recent_lows else l
    swing_high = max(recent_highs) if recent_highs else h

    signal: Optional[str] = None

    if trend_up and (touch_fast_up or touch_slow_up) and bullish:
        signal = "long"
    elif trend_down and (touch_fast_dn or touch_slow_dn) and bearish:
        signal = "short"

    info = {
        "ema_fast": float(state.ema20),
        "ema_slow": float(state.ema50),
        "ema_trend": float(state.ema200),
        "swing_low": float(swing_low),
        "swing_high": float(swing_high),
        "touch_fast": 1.0 if (touch_fast_up or touch_fast_dn) else 0.0,
        "touch_slow": 1.0 if (touch_slow_up or touch_slow_dn) else 0.0,
    }

    return state, signal, info
