"""
StockVest — ml/scoring.py
Hybrid scoring: GradientBoosting ML model (60%) + technical composite (40%).

Technical composite (0–100):
  RSI(14)          25% — momentum
  MACD trend       20% — trend direction
  Price momentum   20% — 5d/20d returns
  Bollinger Band   15% — mean-reversion positioning
  Volume surge     10% — conviction signal
  MA crossover     10% — trend structure (Golden/Death cross)

ML model (GradientBoosting, trained on 5y Nifty50 data):
  P(stock beats Nifty by >5% in next 30 days) → 0–100 score

Final score = 0.6 × ML + 0.4 × Technical  (falls back to 100% technical if model missing)
All results cached 4 hours via data.cache.Cache.
"""
import asyncio
import logging
from typing import Optional

from data.cache import Cache

logger = logging.getLogger(__name__)
_SCORE_TTL = 4 * 3600  # 4 hours


# ── Technical indicator helpers ───────────────────────────────
def _rsi(closes, period=14):
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in deltas]
    losses = [abs(min(d, 0)) for d in deltas]

    def ema(vals, p):
        if len(vals) < p:
            return sum(vals) / len(vals) if vals else 0
        avg = sum(vals[:p]) / p
        k   = 2 / (p + 1)
        for v in vals[p:]:
            avg = v * k + avg * (1 - k)
        return avg

    avg_gain = ema(gains, period)
    avg_loss = ema(losses, period)
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _ema(closes, span):
    if not closes:
        return 0.0
    k   = 2 / (span + 1)
    val = closes[0]
    for c in closes[1:]:
        val = c * k + val * (1 - k)
    return val


def _sma(closes, period):
    if len(closes) < period:
        return sum(closes) / len(closes) if closes else 0
    return sum(closes[-period:]) / period


def _bollinger(closes, period=20):
    if len(closes) < period:
        mid = sum(closes) / len(closes)
        std = 0
    else:
        window = closes[-period:]
        mid    = sum(window) / period
        std    = (sum((x - mid) ** 2 for x in window) / period) ** 0.5
    upper = mid + 2 * std
    lower = mid - 2 * std
    return upper, mid, lower


def _compute_score(closes: list, volumes: list) -> dict:
    """
    Compute composite score 0-100 from raw price/volume lists.
    Returns dict with score and individual component signals.
    """
    if len(closes) < 30:
        return {"score": 50, "components": {}, "error": "insufficient data"}

    price = closes[-1]
    comps = {}
    total_weight = 0
    weighted_sum = 0

    # ── 1. RSI (weight 25) ───────────────────────────────────
    rsi = _rsi(closes[-60:] if len(closes) >= 60 else closes)
    # RSI score: oversold (0-30) = bullish 80-100, overbought (70-100) = bearish 0-30
    if rsi < 30:
        rsi_score = 85 + (30 - rsi) * 0.5   # oversold → strong buy
    elif rsi < 50:
        rsi_score = 50 + (50 - rsi) * 1.75  # below midline → moderate buy
    elif rsi < 70:
        rsi_score = 70 - (rsi - 50) * 1.0   # approaching overbought
    else:
        rsi_score = 30 - (rsi - 70) * 0.5   # overbought → caution
    rsi_score = max(0, min(100, rsi_score))
    comps["rsi"]   = {"value": round(rsi, 1), "score": round(rsi_score, 1)}
    weighted_sum  += rsi_score * 25
    total_weight  += 25

    # ── 2. MACD (weight 20) ──────────────────────────────────
    ema12  = _ema(closes, 12)
    ema26  = _ema(closes, 26)
    macd   = ema12 - ema26
    # Use shorter series for signal to approximate 9-period EMA of MACD
    recent = closes[-35:] if len(closes) >= 35 else closes
    macds  = [(_ema(recent[:i+1], 12) - _ema(recent[:i+1], 26)) for i in range(len(recent))]
    signal = _ema(macds, 9) if len(macds) >= 9 else (macds[-1] if macds else 0)
    hist   = macd - signal
    # Score: positive histogram + macd > signal = bullish
    if hist > 0 and macd > 0:
        macd_score = 75 + min(hist / (abs(price) * 0.001 + 1e-9) * 10, 25)
    elif hist > 0:
        macd_score = 60 + min(hist / (abs(price) * 0.001 + 1e-9) * 5, 15)
    elif hist < 0 and macd < 0:
        macd_score = 25 - min(abs(hist) / (abs(price) * 0.001 + 1e-9) * 10, 25)
    else:
        macd_score = 40
    macd_score = max(0, min(100, macd_score))
    comps["macd"] = {"macd": round(macd, 4), "signal": round(signal, 4),
                     "hist": round(hist, 4), "score": round(macd_score, 1)}
    weighted_sum += macd_score * 20
    total_weight += 20

    # ── 3. Price momentum (weight 20) ────────────────────────
    ret5  = (closes[-1] / closes[-6]  - 1) * 100 if len(closes) >= 6  else 0
    ret20 = (closes[-1] / closes[-21] - 1) * 100 if len(closes) >= 21 else 0
    # Blend 5d and 20d momentum — penalise overbought (>15% in 20d)
    mom = ret5 * 0.4 + ret20 * 0.6
    if mom > 15:
        mom_score = 65 + (mom - 15) * 0.3
    elif mom > 0:
        mom_score = 55 + mom * 0.67
    elif mom > -10:
        mom_score = 55 + mom * 1.5
    else:
        mom_score = 40 + mom * 0.5
    mom_score = max(0, min(100, mom_score))
    comps["momentum"] = {"ret5d": round(ret5, 2), "ret20d": round(ret20, 2),
                         "score": round(mom_score, 1)}
    weighted_sum += mom_score * 20
    total_weight += 20

    # ── 4. Bollinger Band position (weight 15) ────────────────
    bb_upper, bb_mid, bb_lower = _bollinger(closes)
    bb_range = bb_upper - bb_lower
    if bb_range > 0:
        bb_pct = (price - bb_lower) / bb_range   # 0 = at lower, 1 = at upper
    else:
        bb_pct = 0.5
    # Near lower band = oversold setup → bullish; near upper = overbought → bearish
    if bb_pct < 0.2:
        bb_score = 80 + (0.2 - bb_pct) * 100
    elif bb_pct < 0.5:
        bb_score = 60 + (0.5 - bb_pct) * 67
    elif bb_pct < 0.8:
        bb_score = 60 - (bb_pct - 0.5) * 67
    else:
        bb_score = 20 - (bb_pct - 0.8) * 100
    bb_score = max(0, min(100, bb_score))
    comps["bollinger"] = {"pct": round(bb_pct * 100, 1), "score": round(bb_score, 1)}
    weighted_sum += bb_score * 15
    total_weight += 15

    # ── 5. Volume surge (weight 10) ──────────────────────────
    if len(volumes) >= 20:
        avg_vol20 = sum(volumes[-20:]) / 20
        last_vol  = volumes[-1]
        vol_ratio = last_vol / avg_vol20 if avg_vol20 > 0 else 1
        if vol_ratio > 2.0 and ret5 > 0:     # volume surge + price up = strong signal
            vol_score = 85 + min((vol_ratio - 2) * 5, 15)
        elif vol_ratio > 1.5 and ret5 > 0:
            vol_score = 70
        elif vol_ratio > 1.0:
            vol_score = 55
        elif vol_ratio > 0.5:
            vol_score = 45
        else:
            vol_score = 30
    else:
        vol_score = 50
    vol_score = max(0, min(100, vol_score))
    comps["volume"] = {"ratio": round(vol_ratio if len(volumes) >= 20 else 1, 2),
                       "score": round(vol_score, 1)}
    weighted_sum += vol_score * 10
    total_weight += 10

    # ── 6. MA crossover (weight 10) ──────────────────────────
    ma50  = _sma(closes, 50)
    ma200 = _sma(closes, min(200, len(closes)))
    above_50  = price > ma50
    golden    = ma50 > ma200   # golden cross = bullish
    price_vs50 = (price - ma50) / ma50 * 100 if ma50 else 0

    if golden and above_50:
        ma_score = 80 + min(price_vs50 * 0.5, 20)
    elif golden:
        ma_score = 60
    elif above_50:
        ma_score = 50
    else:
        ma_score = 25 - min(abs(price_vs50) * 0.3, 25)
    ma_score = max(0, min(100, ma_score))
    comps["ma"] = {"ma50": round(ma50, 2), "ma200": round(ma200, 2),
                   "golden_cross": golden, "score": round(ma_score, 1)}
    weighted_sum += ma_score * 10
    total_weight += 10

    # ── Final composite score ─────────────────────────────────
    final = round(weighted_sum / total_weight) if total_weight else 50
    final = max(5, min(98, final))

    signal = "BUY" if final >= 75 else "HOLD" if final >= 50 else "CAUTION"

    return {
        "score":      final,
        "signal":     signal,
        "rsi":        round(rsi, 1),
        "macd":       round(macd, 4),
        "macd_signal":round(signal if isinstance(signal, float) else 0, 4),
        "ma50":       round(ma50, 2),
        "ma200":      round(ma200, 2),
        "golden_cross": golden,
        "bb_pct":     round(bb_pct * 100, 1),
        "ret5d":      round(ret5, 2),
        "ret20d":     round(ret20, 2),
        "components": comps,
        "real_data":  True,
    }


async def compute_score(symbol: str) -> dict:
    """
    Public API: compute (or return cached) hybrid ML + technical score for a symbol.
    Fetches real 6-month OHLCV from yfinance.
    Falls back to 50 if data unavailable.
    """
    cache_key = f"score:{symbol}"
    cached = await Cache.get(cache_key)
    if cached:
        return cached

    def _sync():
        try:
            import yfinance as yf
            for suffix in [".NS", ".BO"]:
                tk = yf.Ticker(f"{symbol}{suffix}")
                df = tk.history(period="6mo", auto_adjust=True)
                if not df.empty and len(df) >= 30:
                    break
            if df.empty or len(df) < 30:
                return None
            closes  = [float(v) for v in df["Close"].tolist()]
            volumes = [int(v) for v in df["Volume"].tolist()]
            tech = _compute_score(closes, volumes)

            # Blend with ML model prediction if available
            try:
                from ml.trainer import get_ml_prediction
                ml_raw = get_ml_prediction(symbol, closes, volumes)
                if ml_raw is not None:
                    # ml_raw is already 0-100 (model.score returns 0-100 probabilities)
                    blended = round(0.6 * ml_raw + 0.4 * tech["score"])
                    blended = max(5, min(98, blended))
                    tech["score"]    = blended
                    tech["ml_model"] = round(ml_raw, 1)
                    tech["ml_blend"] = True
                    # Re-derive signal from blended score
                    tech["signal"] = "BUY" if blended >= 75 else "HOLD" if blended >= 50 else "CAUTION"
            except Exception:
                pass  # ML model not ready — use pure technical

            return tech
        except Exception as e:
            logger.warning(f"Score computation failed for {symbol}: {e}")
            return None

    loop   = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _sync)

    if result is None:
        result = {"score": 50, "signal": "HOLD", "real_data": False,
                  "error": "data unavailable"}

    await Cache.set(cache_key, result, ttl=_SCORE_TTL)
    return result


async def score_batch(symbols: list) -> dict:
    """Score multiple symbols concurrently."""
    tasks  = [compute_score(sym) for sym in symbols]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return {sym: (r if not isinstance(r, Exception) else {"score": 50, "signal": "HOLD"})
            for sym, r in zip(symbols, results)}
