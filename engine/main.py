import time
import json
import os
from datetime import datetime, timezone
from engine.strategy_ema import EmaState, on_price
from engine.state import new_state, utc_day, Position
from engine.marketdata import get_binance_price

CONFIG_PATH = "config/settings.json"

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def log_event(event: dict) -> None:
    os.makedirs("logs", exist_ok=True)
    event["ts"] = datetime.now(timezone.utc).isoformat()
    with open("logs/engine.log", "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")

def main():
    config = load_config()
    symbol = config["symbols"][0]
    interval = int(config["interval_sec"])

    initial_equity = float(config.get("initial_equity", 100.0))
    state = new_state(initial_equity)


    strat_cfg = config.get("strategy", {})
    ema_fast = int(strat_cfg.get("ema_fast", 12))
    ema_slow = int(strat_cfg.get("ema_slow", 26))
    max_hold_ticks = int(strat_cfg.get("max_hold_ticks", 60))
    strat_state = EmaState()
    hold_ticks = 0


    log_event({"type": "startup", "config": config})

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
            price = get_binance_price(symbol)
        except Exception as e:
            log_event({
                    "type": "marketdata_error",
                    "symbol": symbol,
                    "error": str(e)
            })
            time.sleep(interval)
            continue

        log_event({
        "type": "marketdata_ok",
        "symbol": symbol,
        "price": price
        })

        log_event({
        "type": "tick",
        "counter": counter,
        "symbol": symbol,
        "mode": config["mode"],
        "price": price,
        "trades_today": state.trades_today,
        "has_position": state.position is not None,
        "equity": state.equity,
        "daily_pnl": state.daily_pnl
        })

        if not config.get("trade_enabled", False):
            time.sleep(interval)
            continue

        # Strategy update: get signal
        strat_state, signal = on_price(strat_state, price, ema_fast, ema_slow)

        log_event({
            "type": "strategy_state",
            "ema_fast": strat_state.ema_fast,
            "ema_slow": strat_state.ema_slow,
            "signal": signal
        })

        # Entry
        if state.position is None and signal == "long":
            risk_pct = float(config.get("risk_per_trade_pct", 1.0)) / 100.0
            stop_pct = float(strat_cfg.get("stop_loss_pct", 1.0)) / 100.0
            stop_distance = price * stop_pct
            risk_amount = state.equity * risk_pct



            # Schutz gegen Division by zero
            if stop_distance <= 0:
                log_event({
		    "type": "entry_skipped",
		    "reason": "bad_stop_distance",
		    "stop_distance": stop_distance
		})
                time.sleep(interval)
                continue

            size = risk_amount / stop_distance
            stop_price = price * (1.0 - stop_pct)

            state.position = Position(
                symbol=symbol,
                side="long",
                entry_price=price,
                size=size,
                opened_at=datetime.now(timezone.utc).isoformat(),
                stop_price=stop_price
            )



            state.trades_today += 1
            hold_ticks = 0
            log_event({
                "type": "position_opened",
                "symbol": symbol,
                "side": "long",
                "entry_price": price,
                "size": size,
                "stop_price": stop_price,
                "equity_before": state.equity,
                "risk_pct": risk_pct,
                "risk_amount": risk_amount,
                "reason": "ema_cross_long"
            })

        # Exit: Stop-Loss
        if state.position is not None:
            if price <= state.position.stop_price:
                pnl = (price - state.position.entry_price) * state.position.size
                state.equity += pnl
                state.daily_pnl += pnl

                log_event({
                    "type": "position_closed",
                    "symbol": symbol,
                    "side": state.position.side,
                    "entry_price": state.position.entry_price,
                    "exit_price": price,
                    "size": state.position.size,
                    "stop_price": state.position.stop_price,
                    "pnl": pnl,
                    "equity_after": state.equity,
                    "daily_pnl": state.daily_pnl,
                    "reason": "stop_loss"
                })

                state.position = None
                hold_ticks = 0
                time.sleep(interval)
                continue


        # Exit: time-based for now
        if state.position is not None:
            hold_ticks += 1
            if hold_ticks >= max_hold_ticks:
                pnl = (price - state.position.entry_price) * state.position.size
                state.equity += pnl
		state.daily_pnl += pnl

		log_event({
                    "type": "position_closed",
                    "symbol": symbol,
                    "side": state.position.side,
                    "entry_price": state.position.entry_price,
                    "exit_price": price,
                    "pnl": pnl,
                    "equity_after": state.equity,
                    "daily_pnl": state.daily_pnl,
                    "reason": "time_exit"
           	 })
                state.position = None
                hold_ticks = 0


        # Risk gate: max trades/day
        if state.trades_today >= int(config["max_trades_per_day"]):
            time.sleep(interval)
            continue

if __name__ == "__main__":
    main()

