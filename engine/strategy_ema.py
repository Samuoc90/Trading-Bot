from dataclasses import dataclass

@dataclass
class EmaState:
    ema_fast: float | None = None
    ema_slow: float | None = None
    last_signal: str | None = None  # "long", "short", None

def ema_update(prev: float | None, price: float, period: int) -> float:
    alpha = 2.0 / (period + 1.0)
    if prev is None:
        return price
    return prev + alpha * (price - prev)

def on_price(state: EmaState, price: float, fast: int, slow: int) -> tuple[EmaState, str | None]:
    # Update EMAs
    state.ema_fast = ema_update(state.ema_fast, price, fast)
    state.ema_slow = ema_update(state.ema_slow, price, slow)

    # Need both
    if state.ema_fast is None or state.ema_slow is None:
        return state, None

    # Signal when crossing
    if state.ema_fast > state.ema_slow and state.last_signal != "long":
        state.last_signal = "long"
        return state, "long"

    if state.ema_fast < state.ema_slow and state.last_signal != "short":
        state.last_signal = "short"
        return state, "short"

    return state, None
