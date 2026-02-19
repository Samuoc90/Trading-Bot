import time
import json
import os
from datetime import datetime, timezone
from engine.strategy_ema import EmaState, on_price
from engine.state import new_state, utc_day, Position
from engine.marketdata import get_binance_price
from engine.marketdata import get_binance_price, get_binance_last_closed_candle
from engine.strategy_trend import TrendPullbackState, on_candle

CONFIG_PATH = "config/settings.json"

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def log_event(event: dict) -> None:
    os.makedirs("logs", exist_ok=True)
    event["ts"] = datetime.now(timezone.utc).isoformat()
    with open("logs/engine.log", "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")

def apply_costs(pnl: float, notional: float, fee_bps: float, slippage_bps: float) -> tuple[float, float]:
    """
    Costs are applied on notional (entry+exit) approximation.
    notional should be roughly: abs(price) * size
    Returns: (net_pnl, total_cost)
    """
    rate = (fee_bps + slippage_bps) / 10000.0
    # round-trip approximation: entry + exit ~ 2 * notional
    total_cost = 2.0 * notional * rate
    net_pnl = pnl - total_cost
    return net_pnl, total_cost

def close_position(state, pos, exit_price_signal: float, reason: str, fee_rate: float, slip_rate: float, log_event) -> None:
    """
    Exit-Accounting:
    - Entry-Fee wurde beim Open bereits von equity abgezogen.
    - Beim Close ziehen wir Exit-Fee ab und rechnen PnL mit Fill-Preisen.
    """
    # Exit fill with slippage (gegen dich)
    if pos.side == "long":
        exit_fill = exit_price_signal * (1.0 - slip_rate)
        exit_slippage = exit_price_signal - exit_fill
        qty_btc = pos.notional_usdt / pos.entry_price  # entry_price ist bereits entry_fill
        pnl_gross = (exit_fill - pos.entry_price) * qty_btc
    else:  # short
        exit_fill = exit_price_signal * (1.0 + slip_rate)
        exit_slippage = exit_fill - exit_price_signal
        qty_btc = pos.notional_usdt / pos.entry_price
        pnl_gross = (pos.entry_price - exit_fill) * qty_btc

    # Exit fee
    exit_fee = pos.notional_usdt * fee_rate

    # Net PnL: entry fee already paid at open
    pnl_net = pnl_gross - exit_fee

    # Apply
    equity_before = state.equity
    state.equity += pnl_net
    state.daily_pnl = state.equity - state.day_start_equity

    log_event({
        "type": "position_closed",
        "symbol": pos.symbol,
        "side": pos.side,
        "reason": reason,

        "signal_exit_price": exit_price_signal,
        "exit_price": exit_fill,
        "exit_slippage": exit_slippage,

        "entry_price": pos.entry_price,
        "entry_fee": getattr(pos, "entry_fee", None),
        "entry_slippage": getattr(pos, "entry_slippage", None),

        "notional_usdt": pos.notional_usdt,
        "qty_btc": qty_btc,

        "stop_price": pos.stop_price,
        "take_profit_price": pos.take_profit_price,

        "pnl_gross": pnl_gross,
        "exit_fee": exit_fee,
        "pnl_net": pnl_net,

        "equity_before": equity_before,
        "equity_after": state.equity,
        "daily_pnl": state.daily_pnl
    })

    state.position = None

def main():
    config = load_config()
    costs_cfg = config.get("costs", {})
    fee_bps = float(costs_cfg.get("fee_bps", 0.0))
    slippage_bps = float(costs_cfg.get("slippage_bps", 0.0))

    symbol = config["symbols"][0]
    interval = int(config["interval_sec"])

    initial_equity = float(config.get("initial_equity", 100.0))
    state = new_state(initial_equity)


    strat_cfg = config.get("strategy", {})
    strategy_name = str(strat_cfg.get("name", "ema_cross"))

    ema_trend = int(strat_cfg.get("ema_trend", 200))
    ema_fast_pb = int(strat_cfg.get("ema_pullback_fast", 20))
    ema_slow_pb = int(strat_cfg.get("ema_pullback_slow", 50))
    pullback_band_pct = float(strat_cfg.get("pullback_band_pct", 0.0015))
    swing_lookback = int(strat_cfg.get("swing_lookback", 5))
    rr_takeprofit = float(strat_cfg.get("rr_takeprofit", 2.0))

    ema_fast = int(strat_cfg.get("ema_fast", 12))
    ema_slow = int(strat_cfg.get("ema_slow", 26))
    max_hold_candles = int(strat_cfg.get("max_hold_candles", 12))

    # Costs config
    cost_cfg = config.get("costs", {})
    fee_rate = float(cost_cfg.get("fee_bps", 8.0)) / 10_000.0
    slip_rate = float(cost_cfg.get("slippage_bps", 2.0)) / 10_000.0


    hold_candles = 0
    trend_state = TrendPullbackState()

    use_candles = bool(strat_cfg.get("use_candles", False))
    candle_interval = str(strat_cfg.get("candle_interval", "1m"))
    last_candle_close_time = None


    log_event({"type": "startup", "config": config})
    last_candle = None
    counter = 0
    while True:
        counter += 1

        # Tageswechsel UTC -> trade counter reset
        today = utc_day()
        if today != state.day_utc:
            log_event({
            "type": "day_rollover",
            "from": state.day_utc,
            "to": today
            })
            state.day_utc = today
            state.trades_today = 0
            state.day_start_equity = state.equity
            state.daily_pnl = 0.0

        #Daily Loss Kill-Switch
        max_daily_loss_pct = float(config.get("max_daily_loss_pct", 5.0)) / 100.0
        daily_loss_limit = -max_daily_loss_pct * state.day_start_equity

        if state.daily_pnl <= daily_loss_limit:
            log_event({
                "type": "daily_loss_limit_hit",
                "day_start_equity": state.day_start_equity,
                "daily_pnl": state.daily_pnl,
                "limit": daily_loss_limit
            })
            time.sleep(interval)
            continue

        try:
            if use_candles:
                candle = get_binance_last_closed_candle(symbol, candle_interval)

                # nur weiterarbeiten, wenn neue Kerze abgeschlossen wurde
                if candle["close_time"] == last_candle_close_time:
                    time.sleep(interval)
                    continue

                last_candle_close_time = candle["close_time"]
                price = candle["close"]  # wir arbeiten mit Close

                log_event({
                    "type": "candle_ok",
                    "symbol": symbol,
                    "interval": candle_interval,
                    **candle
                })
                last_candle = candle
            else:
                price = get_binance_price(symbol)
                log_event({
                    "type": "marketdata_ok",
                    "symbol": symbol,
                    "price": price
                })
        except Exception as e:
            log_event({
                "type": "marketdata_error",
                "symbol": symbol,
                "error": str(e)
            })
            time.sleep(interval)
            continue


        log_event({
        "type": "tick",
        "counter": counter,
        "symbol": symbol,
        "mode": config["mode"],
        "price": price,
        "candle_interval": candle_interval if use_candles else None,
        "trades_today": state.trades_today,
        "has_position": state.position is not None,
        "equity": state.equity,
        "daily_pnl": state.daily_pnl
        })

        if not config.get("trade_enabled", False):
            time.sleep(interval)
            continue

        # Risk gate: max trades/day (block new entries, but still manage exits)
        if state.trades_today >= int(config["max_trades_per_day"]):
            # wir lassen Exits trotzdem laufen -> deshalb: NICHT hier continue, wenn Position offen
            if state.position is None:
                time.sleep(interval)
                continue


        # Strategy update
        signal = None
        info = {}

        if strategy_name == "trend_pullback":
            trend_state, signal, info = on_candle(
                trend_state,
                last_candle,
                ema_trend=ema_trend,
                ema_fast=ema_fast_pb,
                ema_slow=ema_slow_pb,
                pullback_band_pct=pullback_band_pct,
                swing_lookback=swing_lookback
            )

            log_event({
                "type": "strategy_state",
                "strategy": "trend_pullback",
                "ema_fast": info.get("ema_fast"),
                "ema_slow": info.get("ema_slow"),
                "ema_trend": info.get("ema_trend"),
                "signal": signal,
                "swing_low": info.get("swing_low"),
                "swing_high": info.get("swing_high"),
                "touch_fast": info.get("touch_fast"),
                "touch_slow": info.get("touch_slow")
            })
        else:
            # fallback: EMA crossover (falls du es noch behalten willst)
            strat_state, signal = on_price(strat_state, price, ema_fast, ema_slow)
            log_event({
                "type": "strategy_state",
                "strategy": "ema_cross",
                "ema_fast": strat_state.ema_fast,
                "ema_slow": strat_state.ema_slow,
                "signal": signal
            })

        # Entry (long + short)
        if state.position is None and signal in ("long", "short"):

            # Stop bestimmen (strategieabhängig)
            if strategy_name == "trend_pullback":
                if signal == "long":
                    stop_price = float(info["swing_low"])
                else:  # short
                    stop_price = float(info["swing_high"])
            else:
                stop_pct = float(strat_cfg.get("stop_loss_pct", 1.0)) / 100.0
                stop_price = price * (1.0 - stop_pct) if signal == "long" else price * (1.0 + stop_pct)

            risk_cfg = config.get("risk", {})
            if "risk" not in config:
                log_event({"type": "config_warning", "message": "Missing 'risk' block in settings.json. Using defaults."})
            risk_pct = float(risk_cfg.get("risk_per_trade_pct", 1.0)) / 100.0
            max_leverage = float(risk_cfg.get("max_leverage", 3.0))

            risk_amount = state.equity * risk_pct

            stop_distance = abs(price - stop_price)

            if stop_distance <= 0:
                log_event({"type": "entry_skipped", "reason": "bad_stop_distance"})
                time.sleep(interval)
                continue

            # 👉 Notional berechnen (saubere Formel)
            notional_usdt = risk_amount * price / stop_distance

            # 👉 Leverage-Constraint (echte Margin-Logik)
            max_notional = state.equity * max_leverage
            notional_usdt = min(notional_usdt, max_notional)

            # optional: falls notional extrem klein/0 wird
            if notional_usdt <= 0:
                log_event({"type": "entry_skipped", "reason": "notional_le_0", "notional_usdt": notional_usdt})
                time.sleep(interval)
                continue


            # Take Profit (RR)
            rr_takeprofit = float(strat_cfg.get("rr_takeprofit", 2.0))
            if signal == "long":
                take_profit_price = price + rr_takeprofit * (price - stop_price)
            else:  # short
                take_profit_price = price - rr_takeprofit * (stop_price - price)

            equity_before = state.equity

            # --- Entry costs (slippage + fee) ---
            # entry fill with slippage
            if signal == "long":
                entry_fill = price * (1.0 + slip_rate)
                entry_slippage = entry_fill - price
            else:  # short
                entry_fill = price * (1.0 - slip_rate)
                entry_slippage = price - entry_fill

            # pay entry fee immediately
            entry_fee = notional_usdt * fee_rate
            state.equity -= entry_fee
            state.daily_pnl = state.equity - state.day_start_equity


            state.position = Position(
                symbol=symbol,
                side=signal,
                entry_price=entry_fill,
                notional_usdt=notional_usdt,
                opened_at=datetime.now(timezone.utc).isoformat(),
                stop_price=stop_price,
                take_profit_price=take_profit_price,
                entry_fee=entry_fee,
                entry_slippage=entry_slippage
            )


            state.trades_today += 1
            hold_candles = 0

            log_event({
                "type": "position_opened",
                "symbol": symbol,
                "side": signal,
                "signal_price": price,
                "entry_price": entry_fill,
                "notional_usdt": notional_usdt,
                "qty_btc": notional_usdt / entry_fill,
                "stop_price": stop_price,
                "take_profit_price": take_profit_price,
                "entry_fee": entry_fee,
                "entry_slippage": entry_slippage,
                "equity_before": equity_before,          # falls du das vorher speicherst
                "equity_after": state.equity,
                "reason": "trend_pullback_" + signal
            })

        # Exit management (TP / SL / Time)
        if state.position is not None:
            pos = state.position

        # Fees/Slippage aus config
        costs_cfg = config.get("costs", {})
        fee_rate = float(costs_cfg.get("fee_bps", 8.0)) / 10000.0
        slip_rate = float(costs_cfg.get("slippage_bps", 2.0)) / 10000.0

        # TP/SL hit?
        tp_hit = (
            (pos.side == "long" and price >= pos.take_profit_price) or
            (pos.side == "short" and price <= pos.take_profit_price)
        )
        sl_hit = (
            (pos.side == "long" and price <= pos.stop_price) or
            (pos.side == "short" and price >= pos.stop_price)
        )

        if sl_hit:
            close_position(state, pos, pos.stop_price, "stop_loss", fee_rate, slip_rate, log_event)
            hold_candles = 0
            time.sleep(interval)
            continue

        if tp_hit:
            close_position(state, pos, pos.take_profit_price, "take_profit", fee_rate, slip_rate, log_event)
            hold_candles = 0
            time.sleep(interval)
            continue

        # Time exit (count only while position is open)
        hold_candles += 1
        if hold_candles >= max_hold_candles:
            close_position(state, pos, price, "time_exit", fee_rate, slip_rate, log_event)
            hold_candles = 0
            time.sleep(interval)
            continue


    time.sleep(interval)

if __name__ == "__main__":
    main()
