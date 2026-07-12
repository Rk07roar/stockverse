"""
Alladin — api/signals.py
Real-time BUY/SELL signal engine using:
  • RSI(14)       — momentum oscillator
  • MACD(12,26,9) — trend + momentum crossover
  • Bollinger Bands(20,2σ) — volatility + mean reversion
  • EMA 20/50     — trend direction
  • Volume ratio  — institutional confirmation

Signal scoring: each indicator contributes -2 to +2.
Total score > +3 = BUY, < -3 = SELL, else WATCH.
"""
import asyncio
import logging
from typing import List, Dict, Optional
from fastapi import APIRouter
from data.fetcher import DataFetcher
from data.cache import Cache

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Technical indicator helpers ────────────────────────────────

def _rsi(closes: list, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas[-period:]]
    losses = [abs(min(d, 0)) for d in deltas[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def _ema(closes: list, period: int) -> list:
    if len(closes) < period:
        return []
    k = 2 / (period + 1)
    ema = [sum(closes[:period]) / period]
    for price in closes[period:]:
        ema.append(price * k + ema[-1] * (1 - k))
    return ema


def _macd(closes: list):
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    if not ema12 or not ema26:
        return None, None, None
    # Align lengths
    diff = len(ema12) - len(ema26)
    ema12 = ema12[diff:]
    macd_line = [e12 - e26 for e12, e26 in zip(ema12, ema26)]
    signal = _ema(macd_line, 9)
    if not signal:
        return None, None, None
    offset = len(macd_line) - len(signal)
    histogram = [m - s for m, s in zip(macd_line[offset:], signal)]
    return macd_line[-1], signal[-1], histogram


def _bollinger(closes: list, period: int = 20):
    if len(closes) < period:
        return None, None, None
    window = closes[-period:]
    mid = sum(window) / period
    variance = sum((p - mid) ** 2 for p in window) / period
    std = variance ** 0.5
    return round(mid - 2 * std, 2), round(mid, 2), round(mid + 2 * std, 2)


def _score_stock(closes: list, volumes: list, current_price: float) -> Dict:
    """Run all indicators and return a scored signal dict."""
    reasons = []
    score = 0

    # ── RSI ──────────────────────────────────────────────────
    rsi = _rsi(closes)
    if rsi < 30:
        score += 2; reasons.append(f"RSI {rsi:.0f} — deeply oversold (strong buy zone)")
    elif rsi < 40:
        score += 1; reasons.append(f"RSI {rsi:.0f} — oversold")
    elif rsi > 70:
        score -= 2; reasons.append(f"RSI {rsi:.0f} — overbought (take profit zone)")
    elif rsi > 60:
        score -= 1; reasons.append(f"RSI {rsi:.0f} — approaching overbought")

    # ── MACD ─────────────────────────────────────────────────
    macd_val, signal_val, histogram = _macd(closes)
    if macd_val is not None and signal_val is not None and histogram:
        cross = macd_val - signal_val
        prev_cross = histogram[-2] if len(histogram) >= 2 else cross
        if cross > 0 and prev_cross <= 0:
            score += 2; reasons.append("MACD bullish crossover — momentum turning up")
        elif cross < 0 and prev_cross >= 0:
            score -= 2; reasons.append("MACD bearish crossover — momentum turning down")
        elif cross > 0 and histogram[-1] > 0:
            score += 1; reasons.append("MACD above signal — upward momentum")
        elif cross < 0 and histogram[-1] < 0:
            score -= 1; reasons.append("MACD below signal — downward momentum")

    # ── Bollinger Bands ───────────────────────────────────────
    bb_lower, bb_mid, bb_upper = _bollinger(closes)
    if bb_lower and bb_upper:
        bb_width = bb_upper - bb_lower
        if current_price <= bb_lower:
            score += 2; reasons.append("Price at lower Bollinger Band — mean reversion buy")
        elif current_price >= bb_upper:
            score -= 2; reasons.append("Price at upper Bollinger Band — extended, watch for pullback")
        elif current_price < bb_mid and (bb_mid - current_price) / bb_width > 0.3:
            score += 1; reasons.append("Price below BB midline — potential bounce setup")

    # ── EMA trend ────────────────────────────────────────────
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    if ema20 and ema50:
        e20, e50 = ema20[-1], ema50[-1]
        if current_price > e20 > e50:
            score += 1; reasons.append(f"Price > EMA20 > EMA50 — healthy uptrend")
        elif current_price < e20 < e50:
            score -= 1; reasons.append(f"Price < EMA20 < EMA50 — downtrend confirmed")
        # Golden / Death cross
        if len(ema20) >= 2 and len(ema50) >= 2:
            prev_e20 = ema20[-2]; prev_e50 = ema50[-2]
            if e20 > e50 and prev_e20 <= prev_e50:
                score += 2; reasons.append("Golden Cross — EMA20 crossed above EMA50 (major buy signal)")
            elif e20 < e50 and prev_e20 >= prev_e50:
                score -= 2; reasons.append("Death Cross — EMA20 crossed below EMA50 (major sell signal)")

    # ── Volume confirmation ───────────────────────────────────
    if len(volumes) >= 20:
        avg_vol = sum(volumes[-20:]) / 20
        today_vol = volumes[-1]
        price_up = closes[-1] > closes[-2] if len(closes) >= 2 else False
        vol_ratio = today_vol / avg_vol if avg_vol > 0 else 1
        if vol_ratio > 1.5 and price_up:
            score += 1; reasons.append(f"Volume {vol_ratio:.1f}x average — institutional buying confirmed")
        elif vol_ratio > 1.5 and not price_up:
            score -= 1; reasons.append(f"Volume {vol_ratio:.1f}x average on down day — distribution detected")

    # ── Determine signal ─────────────────────────────────────
    if score >= 4:
        signal = "STRONG BUY"
        color = "#00e676"
    elif score >= 2:
        signal = "BUY"
        color = "#69f0ae"
    elif score <= -4:
        signal = "STRONG SELL"
        color = "#ff1744"
    elif score <= -2:
        signal = "SELL"
        color = "#ff5252"
    else:
        signal = "WATCH"
        color = "#ffab40"

    return {
        "score":   score,
        "signal":  signal,
        "color":   color,
        "rsi":     round(rsi, 1),
        "reasons": reasons,
    }


async def _analyze_stock(sym: str, name: str, price: float, change_pct: float) -> Optional[Dict]:
    """Fetch history and run full technical analysis for one stock."""
    try:
        def _fetch():
            import math as _math
            import yfinance as yf
            for suffix in [".NS", ".BO"]:
                tk = yf.Ticker(f"{sym}{suffix}")
                df = tk.history(period="6mo", auto_adjust=True)
                df = df.dropna(subset=["Close"])
                if not df.empty and len(df) >= 50:
                    closes = [float(p) for p in df["Close"].tolist()]
                    if any(_math.isnan(c) or _math.isinf(c) for c in closes):
                        continue
                    volumes = []
                    for v in df["Volume"].tolist():
                        try:
                            volumes.append(int(v))
                        except (ValueError, TypeError):
                            volumes.append(0)
                    return closes, volumes
            return None, None

        loop = asyncio.get_running_loop()
        closes, volumes = await loop.run_in_executor(None, _fetch)
        if not closes:
            return None

        analysis = _score_stock(closes, volumes or [], price)
        return {
            "sym":        sym,
            "name":       name,
            "price":      price,
            "change_pct": change_pct,
            **analysis,
        }
    except Exception as e:
        logger.warning(f"Signal analysis failed for {sym}: {type(e).__name__}: {e}")
        return None


def _atr(highs: list, lows: list, closes: list, period: int = 14) -> float:
    """Average True Range — measures daily price volatility."""
    if len(closes) < 2:
        return closes[-1] * 0.02 if closes else 10.0
    true_ranges = []
    for i in range(1, len(closes)):
        h, lo, pc = highs[i], lows[i], closes[i - 1]
        tr = max(h - lo, abs(h - pc), abs(lo - pc))
        true_ranges.append(tr)
    if not true_ranges:
        return closes[-1] * 0.02
    return sum(true_ranges[-period:]) / min(period, len(true_ranges))


async def _analyze_holding(sym: str, avg_buy_price: float = 0.0) -> Optional[Dict]:
    """
    Full analyst-grade analysis for one portfolio position.
    Returns: verdict (ADD/HOLD/REDUCE/EXIT), price target, stop loss,
    upside %, confidence, RSI, key reasons.
    """
    try:
        def _fetch():
            import math as _math
            import yfinance as yf
            for suffix in [".NS", ".BO"]:
                tk = yf.Ticker(f"{sym}{suffix}")
                df = tk.history(period="6mo", auto_adjust=True)
                # Drop rows with NaN prices — yfinance can return NaN for
                # trading halts / data gaps; NaN would propagate to JSON and
                # cause a 500 (json.dumps rejects float NaN).
                df = df.dropna(subset=["Close", "High", "Low"])
                if not df.empty and len(df) >= 30:
                    closes = [float(p) for p in df["Close"].tolist()]
                    # Guard: reject if any price is still NaN/Inf
                    if any(_math.isnan(c) or _math.isinf(c) for c in closes):
                        continue
                    volumes = []
                    for v in df["Volume"].tolist():
                        try:
                            volumes.append(int(v))
                        except (ValueError, TypeError):
                            volumes.append(0)
                    return {
                        "closes":  closes,
                        "volumes": volumes,
                        "highs":   [float(h) for h in df["High"].tolist()],
                        "lows":    [float(l) for l in df["Low"].tolist()],
                        "high_6m": float(df["High"].max()),
                        "low_6m":  float(df["Low"].min()),
                    }
            return None

        loop = asyncio.get_running_loop()
        raw = await loop.run_in_executor(None, _fetch)
        if not raw:
            return None

        closes  = raw["closes"]
        volumes = raw["volumes"]
        highs   = raw["highs"]
        lows    = raw["lows"]
        high_6m = raw["high_6m"]
        low_6m  = raw["low_6m"]
        current_price = closes[-1]

        # ── Technical scoring (shared engine) ───────────────────
        analysis = _score_stock(closes, volumes, current_price)
        score    = analysis["score"]
        rsi      = analysis["rsi"]
        reasons  = list(analysis["reasons"])

        # ── ATR-based volatility (professional risk sizing) ──────
        atr = _atr(highs, lows, closes)

        # ── Bollinger Bands — support / resistance / targets ─────
        bb_lower, bb_mid, bb_upper = _bollinger(closes)

        # ── Price target & stop loss ─────────────────────────────
        if bb_upper and bb_lower and bb_mid:
            band_width = bb_upper - bb_lower
            if score >= 2:
                price_target = round(bb_upper + band_width * 0.45, 2)
                stop_loss    = round(max(bb_lower - atr * 0.5, current_price - 3.0 * atr), 2)
            elif score <= -2:
                price_target = round(bb_lower - band_width * 0.25, 2)
                stop_loss    = round(current_price - 1.5 * atr, 2)
            else:
                price_target = round(current_price + band_width * 0.38, 2)
                stop_loss    = round(current_price - 2.0 * atr, 2)
        else:
            price_target = round(current_price * 1.08, 2)
            stop_loss    = round(current_price * 0.93, 2)

        # Sanity clamp: target at least +3%, SL between -5% and -20%
        price_target = max(price_target, round(current_price * 1.03, 2))
        stop_loss    = max(stop_loss,    round(current_price * 0.80, 2))
        stop_loss    = min(stop_loss,    round(current_price * 0.97, 2))

        upside_pct = round((price_target - current_price) / current_price * 100, 1)

        # ── P&L context ──────────────────────────────────────────
        pnl_pct = None
        if avg_buy_price and avg_buy_price > 0:
            pnl_pct = round((current_price - avg_buy_price) / avg_buy_price * 100, 1)

        # ── Analyst verdict (technical + P&L context) ────────────
        if score >= 4:
            verdict, verdict_color = "STRONG ADD", "#00e676"
        elif score >= 2:
            if pnl_pct is not None and pnl_pct > 250:
                verdict, verdict_color = "HOLD",       "#40c4ff"
            else:
                verdict, verdict_color = "ADD",        "#69f0ae"
        elif score <= -4:
            if pnl_pct is not None and pnl_pct > 80:
                verdict, verdict_color = "BOOK PROFIT","#ff6d00"
            else:
                verdict, verdict_color = "EXIT",       "#ff1744"
        elif score <= -2:
            if pnl_pct is not None and pnl_pct > 40:
                verdict, verdict_color = "REDUCE",     "#ff9100"
            else:
                verdict, verdict_color = "WATCH",      "#ffab40"
        else:
            if rsi > 68 and (pnl_pct is None or pnl_pct > 30):
                verdict, verdict_color = "TAKE PARTIAL","#ff9100"
            else:
                verdict, verdict_color = "HOLD",       "#40c4ff"

        # ── Confidence: |score| → 45–95% ────────────────────────
        confidence = min(95, max(45, 45 + abs(score) * 9))

        # ── Context-aware reason inserts ─────────────────────────
        if pnl_pct is not None:
            if pnl_pct > 100:
                reasons.insert(0, f"Position +{pnl_pct:.0f}% gain — trail stop loss upward to protect profits")
            elif pnl_pct < -15:
                reasons.insert(0, f"Position -{abs(pnl_pct):.0f}% — review if original investment thesis is still intact")

        if bb_lower and bb_upper:
            reasons.append(f"Support ₹{bb_lower:,.0f} | Resistance ₹{bb_upper:,.0f}")

        # 6-month high/low context
        dist_from_high = (current_price - high_6m) / high_6m * 100
        if dist_from_high < -25:
            reasons.append(f"Trading {abs(dist_from_high):.0f}% below 6M high — potential deep-value entry zone")
        elif dist_from_high > -5:
            reasons.append(f"Near 6M high ₹{high_6m:,.0f} — watch for breakout or reversal")

        return {
            "sym":           sym,
            "price":         round(current_price, 2),
            "price_target":  price_target,
            "stop_loss":     max(round(stop_loss, 2), 1.0),
            "upside_pct":    upside_pct,
            "verdict":       verdict,
            "verdict_color": verdict_color,
            "score":         score,
            "rsi":           rsi,
            "confidence":    confidence,
            "reasons":       reasons[:5],
            "pnl_pct":       pnl_pct,
            "high_6m":       round(high_6m, 2),
            "low_6m":        round(low_6m, 2),
        }

    except Exception as e:
        logger.warning(f"Holding analysis failed for {sym}: {type(e).__name__}: {e}")
        return None


@router.get("/holdings", summary="Analyst-grade recommendations for portfolio holdings")
async def analyze_holdings(symbols: str = "", prices: str = ""):
    """
    symbols: comma-separated e.g. ADANIENSOL,WIPRO,TCS
    prices:  comma-separated avg buy prices for P&L context (same order)
    """
    if not symbols.strip():
        return {"holdings": []}

    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()][:20]
    price_list = [p.strip() for p in (prices or "").split(",")]

    buy_price_map: Dict[str, float] = {}
    for i, sym in enumerate(syms):
        try:
            buy_price_map[sym] = float(price_list[i]) if i < len(price_list) else 0.0
        except Exception:
            buy_price_map[sym] = 0.0

    cache_key = f"signals:holdings:{','.join(sorted(syms))}"
    cached = await Cache.get(cache_key)
    if cached:
        return cached

    tasks = [_analyze_holding(sym, buy_price_map.get(sym, 0.0)) for sym in syms]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    holdings = [r for r in results if isinstance(r, dict)]

    result = {"holdings": holdings}
    await Cache.set(cache_key, result, ttl=120)   # 2-minute cache
    return result


@router.get("/", summary="Live BUY/SELL signals for top stocks")
async def get_signals():
    cached = await Cache.get("signals:live")
    if cached:
        return cached

    # Only analyse stocks with real live data (Nifty 100)
    all_stocks = await DataFetcher.get_all_stocks(sort="ml_desc")
    candidates = [s for s in all_stocks if s.get("real_data")][:40]

    if not candidates:
        # Fallback to top ML-scored stocks even if simulated
        candidates = all_stocks[:20]

    # Run analyses concurrently (cap at 20 to avoid rate limiting)
    tasks = [
        _analyze_stock(s["sym"], s["name"], s["price"], s["change_pct"])
        for s in candidates[:20]
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    signals = [r for r in results if isinstance(r, dict)]

    # Sort: STRONG BUY first, then BUY, then WATCH, then SELL, then STRONG SELL
    order = {"STRONG BUY": 0, "BUY": 1, "WATCH": 2, "SELL": 3, "STRONG SELL": 4}
    signals.sort(key=lambda x: (order.get(x["signal"], 2), -abs(x["score"])))

    result = {"signals": signals, "count": len(signals)}
    await Cache.set("signals:live", result, ttl=300)  # cache 5 min
    return result
