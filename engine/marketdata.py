import requests

def get_binance_price(symbol: str) -> float:
    url = "https://api.binance.com/api/v3/ticker/price"
    r = requests.get(url, params={"symbol": symbol}, timeout=10)
    r.raise_for_status()
    data = r.json()
    return float(data["price"])

def get_binance_last_closed_candle(symbol: str, interval: str = "1m") -> dict:
    """
    Returns last *closed* kline as dict: {open_time, open, high, low, close, volume, close_time}
    Binance klines: https://api.binance.com/api/v3/klines
    We request last 2 and take the first -> closed candle.
    """
    url = "https://api.binance.com/api/v3/klines"
    r = requests.get(url, params={"symbol": symbol, "interval": interval, "limit": 2}, timeout=10)
    r.raise_for_status()
    klines = r.json()
    if len(klines) < 2:
        raise RuntimeError("Not enough klines returned")

    k = klines[0]  # last closed
    return {
        "open_time": int(k[0]),
        "open": float(k[1]),
        "high": float(k[2]),
        "low": float(k[3]),
        "close": float(k[4]),
        "volume": float(k[5]),
        "close_time": int(k[6]),
    }
