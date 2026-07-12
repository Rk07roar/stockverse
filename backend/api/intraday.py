"""
StockVest — api/intraday.py
OHLCV intraday data for the Watch Stocks panel.

GET /api/intraday/{symbol}?period=1d&interval=1m
  Returns time-series OHLCV candles for any symbol + period/interval combo.

Supported combos
  period   interval   use-case
  ─────────────────────────────
  1d       1m         intraday candlestick / live chart
  5d       5m         5-day chart
  1mo      15m        1-month chart
  3mo      1h         3-month chart
  6mo      1d         6-month chart
  1y       1d         1-year chart

All results cached (TTL varies by interval — fast intervals cached 60 s,
daily-bar intervals cached 300 s).
"""
import asyncio
import logging
from fastapi import APIRouter, HTTPException, Query

from data.cache import Cache

logger = logging.getLogger(__name__)
router = APIRouter()

# Cache TTLs per interval
_TTL = {
    "1m":  60,
    "2m":  60,
    "5m":  90,
    "15m": 120,
    "30m": 180,
    "1h":  300,
    "1d":  300,
}

# Fallback intervals to try when the requested one returns no data.
# Coarser intervals are more likely to have data for all stocks/weekends.
_FALLBACKS = {
    "1m":  ["2m", "5m"],
    "2m":  ["5m"],
    "5m":  ["15m", "30m"],
    "15m": ["30m", "1h"],
    "30m": ["1h"],
    "1h":  ["1d"],
}

# Period to use when falling back to a daily interval for short periods
_PERIOD_UPGRADE = {
    "1d": "5d",   # 1d/1m → try 5d/5m if 1m has no data
}


def _try_fetch(symbol: str, period: str, interval: str) -> tuple:
    """
    Try fetching OHLCV for a single (period, interval) combo.
    Returns (df, suffix) or (empty_df, "").
    Requires at least 1 row (was previously >2, which excluded low-volume stocks).
    """
    import yfinance as yf
    import pandas as pd

    for suffix in [".NS", ".BO"]:
        try:
            tk = yf.Ticker(f"{symbol}{suffix}")
            tmp = tk.history(period=period, interval=interval, auto_adjust=True)
            if not tmp.empty and len(tmp) >= 1:
                return tmp, suffix
        except Exception as exc:
            logger.debug("yfinance error %s%s (%s/%s): %s", symbol, suffix, period, interval, exc)

    return pd.DataFrame(), ""


def _fetch_sync(symbol: str, period: str, interval: str) -> dict:
    """
    Download OHLCV from yfinance with automatic fallback.

    Strategy:
      1. Try the exact (period, interval) requested.
      2. If empty, walk through _FALLBACKS[interval] with the same period.
      3. If still empty and period was upgraded (e.g. 1d→5d), try that too.
    """
    df, used_suffix = _try_fetch(symbol, period, interval)
    used_interval   = interval
    used_period     = period

    # Walk through fallback intervals
    if df.empty:
        for fb_interval in _FALLBACKS.get(interval, []):
            fb_period = period
            # For very short periods a coarser interval may need more days
            if fb_period == "1d" and fb_interval in ("5m", "15m", "30m", "1h"):
                fb_period = "5d"
            df, used_suffix = _try_fetch(symbol, fb_period, fb_interval)
            if not df.empty:
                used_interval = fb_interval
                used_period   = fb_period
                logger.info(
                    "Intraday fallback: %s  %s/%s → %s/%s",
                    symbol, period, interval, fb_period, fb_interval,
                )
                break

    if df.empty:
        return {"symbol": symbol, "candles": [], "period": period, "interval": interval}

    candles = []
    for ts, row in df.iterrows():
        try:
            candles.append({
                "t": int(ts.timestamp() * 1000),      # ms epoch
                "o": round(float(row["Open"]),   2),
                "h": round(float(row["High"]),   2),
                "l": round(float(row["Low"]),    2),
                "c": round(float(row["Close"]),  2),
                "v": int(row["Volume"]),
            })
        except Exception:
            pass

    closes = [c["c"] for c in candles]
    highs  = [c["h"] for c in candles]
    lows   = [c["l"] for c in candles]

    return {
        "symbol":           symbol,
        "suffix":           used_suffix,
        "period":           used_period,
        "interval":         used_interval,
        "requested_period": period,
        "requested_interval": interval,
        "count":            len(candles),
        "candles":          candles,
        "summary": {
            "open":       candles[0]["o"]  if candles else 0,
            "close":      candles[-1]["c"] if candles else 0,
            "high":       round(max(highs), 2) if highs else 0,
            "low":        round(min(lows),  2) if lows  else 0,
            "change":     round(candles[-1]["c"] - candles[0]["o"], 2) if candles else 0,
            "change_pct": round((candles[-1]["c"] / candles[0]["o"] - 1) * 100, 2) if candles else 0,
        },
    }


@router.get("/{symbol}", summary="Intraday / historical OHLCV candles")
async def get_intraday(
    symbol:   str,
    period:   str = Query("1d",  description="yfinance period: 1d 5d 1mo 3mo 6mo 1y"),
    interval: str = Query("1m",  description="yfinance interval: 1m 5m 15m 30m 1h 1d"),
):
    sym = symbol.upper().strip()
    cache_key = f"intraday:{sym}:{period}:{interval}"
    ttl = _TTL.get(interval, 120)

    cached = await Cache.get(cache_key)
    if cached:
        return cached

    loop   = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _fetch_sync, sym, period, interval)

    if not result["candles"]:
        raise HTTPException(
            404,
            detail=f"No data available for {sym} — tried {period}/{interval} and coarser fallbacks.",
        )

    await Cache.set(cache_key, result, ttl=ttl)
    return result
