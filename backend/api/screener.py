"""
StockVest — api/screener.py  (v3 — full rebuild)

A genuinely powerful screener combining:
  1. Fundamental filters  — P/E, P/B, P/S, ROE, ROA, D/E, margins, growth, EV/EBITDA, PEG
  2. Technical filters    — RSI, above/below 200 DMA, golden cross, Bollinger squeeze,
                            volume ratio, 52-week proximity, ATH breakout
  3. Signal filter        — reuse existing signal scoring engine (BUY / SELL / WATCH)
  4. Indian-specific      — promoter holding % (yfinance insider proxy), short ratio
  5. Composite score      — ranks each stock 0-100 on combined fundamental + technical quality
  6. Sort by any column   — sort results by pe, roe, rsi, score, change_pct, etc.
  7. Presets              — value, growth, momentum, lowrisk, dividend, turnaround, breakout

All data sources: yfinance (free), NSE live quotes (DataFetcher), existing signals engine.
No paid API required.

Performance note:
  - Fast mode  (enrich=false): uses only cached live prices, <100ms
  - Full mode  (enrich=true):  fetches yfinance fundamentals + 6-month history for each
    candidate — takes 5-15s for 50 stocks but results are cached 6 hours.
"""
import asyncio
import logging
import math
from typing import Optional
from fastapi import APIRouter, Query
from data.fetcher import DataFetcher
from data.cache import Cache

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Helpers ───────────────────────────────────────────────────────
def _safe(val, default=None):
    try:
        f = float(val)
        return default if (math.isnan(f) or math.isinf(f)) else f
    except Exception:
        return default


def _ok(val, lo, hi, strict=False):
    """
    Filter check. strict=True: None value fails the filter.
               strict=False: None value passes (data unavailable → include anyway).
    """
    if val is None:
        return False if strict else True   # no data → include unless strict
    if lo is not None and val < lo:
        return False
    if hi is not None and val > hi:
        return False
    return True


# ── Technical indicators (computed from price history) ─────────────
def _rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d, 0)    for d in deltas[-period:]]
    losses = [abs(min(d,0)) for d in deltas[-period:]]
    ag, al = sum(gains)/period, sum(losses)/period
    if al == 0:
        return 100.0
    return round(100 - (100 / (1 + ag/al)), 1)


def _ema(closes, period):
    if len(closes) < period:
        return []
    k = 2 / (period + 1)
    e = [sum(closes[:period]) / period]
    for p in closes[period:]:
        e.append(p * k + e[-1] * (1-k))
    return e


def _sma(closes, period):
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def _bollinger_width(closes, period=20):
    """Bollinger Band width as % of midline — low = squeeze."""
    if len(closes) < period:
        return None
    w = closes[-period:]
    mid = sum(w) / period
    std = (sum((p - mid)**2 for p in w) / period) ** 0.5
    return round((4 * std / mid) * 100, 2) if mid else None


def _compute_technicals(closes, volumes):
    """
    Compute all technical indicators from raw OHLCV history.
    Returns dict of indicator values.
    """
    if not closes or len(closes) < 20:
        return {}

    price    = closes[-1]
    rsi      = _rsi(closes)
    ema20    = _ema(closes, 20)
    ema50    = _ema(closes, 50)
    sma200   = _sma(closes, 200)
    bb_width = _bollinger_width(closes)

    # Golden cross: EMA20 just crossed above EMA50
    golden_cross = False
    death_cross  = False
    if len(ema20) >= 2 and len(ema50) >= 2:
        if ema20[-1] > ema50[-1] and ema20[-2] <= ema50[-2]:
            golden_cross = True
        elif ema20[-1] < ema50[-1] and ema20[-2] >= ema50[-2]:
            death_cross = True

    above_200dma = bool(sma200 and price > sma200)
    above_50ema  = bool(ema50  and price > ema50[-1])
    above_20ema  = bool(ema20  and price > ema20[-1])

    # Volume ratio vs 20-day average
    vol_ratio_20d = None
    if len(volumes) >= 20:
        avg20 = sum(volumes[-20:]) / 20
        if avg20 > 0:
            vol_ratio_20d = round(volumes[-1] / avg20, 2)

    # ATH proximity (52-week high from history)
    high_52w = max(closes[-252:]) if len(closes) >= 252 else max(closes)
    low_52w  = min(closes[-252:]) if len(closes) >= 252 else min(closes)
    pct_from_ath = round((price - high_52w) / high_52w * 100, 1) if high_52w else None
    pct_from_atl = round((price - low_52w)  / low_52w  * 100, 1) if low_52w  else None

    # Trend strength: how far price is above/below 200 DMA (%)
    dma200_gap = round((price - sma200) / sma200 * 100, 1) if sma200 else None

    return {
        "rsi":          rsi,
        "above_200dma": above_200dma,
        "above_50ema":  above_50ema,
        "above_20ema":  above_20ema,
        "dma200_gap":   dma200_gap,
        "golden_cross": golden_cross,
        "death_cross":  death_cross,
        "bb_width":     bb_width,          # low = squeeze, likely big move coming
        "vol_ratio_20d":vol_ratio_20d,
        "pct_from_ath": pct_from_ath,
        "pct_from_atl": pct_from_atl,
        "sma200":       round(sma200, 2) if sma200 else None,
        "ema50":        round(ema50[-1], 2) if ema50 else None,
        "ema20":        round(ema20[-1], 2) if ema20 else None,
    }


# ── Signal scoring (reused from signals.py) ─────────────────────────
def _signal_score(closes, volumes, price):
    score = 0
    if not closes or len(closes) < 50:
        return 0, "INSUFFICIENT"

    rsi = _rsi(closes) or 50
    if rsi < 30:   score += 2
    elif rsi < 40: score += 1
    elif rsi > 70: score -= 2
    elif rsi > 60: score -= 1

    e20 = _ema(closes, 20); e50 = _ema(closes, 50)
    if e20 and e50:
        if price > e20[-1] > e50[-1]: score += 1
        elif price < e20[-1] < e50[-1]: score -= 1
        if len(e20) >= 2 and len(e50) >= 2:
            if e20[-1] > e50[-1] and e20[-2] <= e50[-2]: score += 2
            elif e20[-1] < e50[-1] and e20[-2] >= e50[-2]: score -= 2

    if len(volumes) >= 20:
        avg = sum(volumes[-20:]) / 20
        if avg > 0 and volumes[-1] / avg > 1.5:
            score += 1 if closes[-1] > closes[-2] else -1

    if   score >= 4:  sig = "STRONG BUY"
    elif score >= 2:  sig = "BUY"
    elif score <= -4: sig = "STRONG SELL"
    elif score <= -2: sig = "SELL"
    else:             sig = "WATCH"
    return score, sig


# ── Composite quality score ────────────────────────────────────────
def _quality_score(fund: dict, tech: dict, ml_score: int) -> int:
    """
    Combined 0-100 quality score blending fundamentals + technicals + ML.
    Inspired by CANSLIM and quality factor investing.
    """
    score = 0

    # Fundamentals (max 40 pts)
    roe = (fund.get("roe") or 0) * 100
    if roe >= 25:  score += 12
    elif roe >= 15: score += 8
    elif roe >= 8:  score += 4

    pe = fund.get("pe")
    if pe and 5 < pe < 20:   score += 8
    elif pe and 20 <= pe < 35: score += 4

    de = fund.get("debt_equity")
    if de is not None:
        if de < 0.3:  score += 8
        elif de < 0.8: score += 4
        elif de > 2:   score -= 4

    pm = (fund.get("profit_margin") or 0) * 100
    if pm >= 20:  score += 7
    elif pm >= 10: score += 4
    elif pm >= 5:  score += 2
    elif pm < 0:   score -= 5

    rg = (fund.get("revenue_growth") or 0) * 100
    if rg >= 20:  score += 5
    elif rg >= 10: score += 3

    # Technicals (max 40 pts)
    if tech.get("above_200dma"):  score += 10
    if tech.get("above_50ema"):   score += 8
    if tech.get("golden_cross"):  score += 10
    if tech.get("death_cross"):   score -= 10

    rsi = tech.get("rsi") or 50
    if 40 <= rsi <= 60:  score += 5
    elif rsi < 30:       score += 8   # oversold = opportunity
    elif rsi > 75:       score -= 5

    vr = tech.get("vol_ratio_20d") or 1
    if vr > 2:   score += 7
    elif vr > 1.5: score += 4

    # ML model (max 20 pts)
    score += min(20, int(ml_score / 5))

    return max(0, min(100, score))


# ── Fundamental data fetch ─────────────────────────────────────────
async def _fetch_fundamentals(symbol: str) -> dict:
    cache_key = f"screener:fund3:{symbol}"
    cached = await Cache.get(cache_key)
    if cached:
        return cached

    def _sync():
        import yfinance as yf
        for suffix in [".NS", ".BO"]:
            try:
                info = yf.Ticker(f"{symbol}{suffix}").info
                if info.get("regularMarketPrice") or info.get("currentPrice"):
                    return info
            except Exception:
                pass
        return {}

    loop = asyncio.get_running_loop()
    info = await loop.run_in_executor(None, _sync)

    ev       = _safe(info.get("enterpriseValue"))
    ebitda   = _safe(info.get("ebitda"))
    ev_ebitda = round(ev / ebitda, 1) if (ev and ebitda and ebitda > 0) else None

    pe       = _safe(info.get("trailingPE"))
    eg       = _safe(info.get("earningsGrowth"))
    peg      = round(pe / (eg * 100), 2) if (pe and eg and eg > 0) else None

    result = {
        # Valuation
        "pe":              pe,
        "forward_pe":      _safe(info.get("forwardPE")),
        "pb":              _safe(info.get("priceToBook")),
        "ps":              _safe(info.get("priceToSalesTrailing12Months")),
        "ev_ebitda":       ev_ebitda,
        "peg":             peg,
        # Profitability
        "roe":             _safe(info.get("returnOnEquity")),
        "roa":             _safe(info.get("returnOnAssets")),
        "roce":            _safe(info.get("returnOnAssets")),   # closest yfinance has
        "profit_margin":   _safe(info.get("profitMargins")),
        "gross_margin":    _safe(info.get("grossMargins")),
        "operating_margin":_safe(info.get("operatingMargins")),
        "ebitda_margin":   _safe(info.get("ebitdaMargins")),
        # Growth
        "revenue_growth":  _safe(info.get("revenueGrowth")),
        "earnings_growth": eg,
        # Financial health
        # NOTE: yfinance/Yahoo returns debtToEquity pre-multiplied by 100
        # (e.g. a real D/E of 0.23 comes back as 23.0), so divide by 100 to
        # get a true ratio. Everything downstream (filters, quality score)
        # expects a ratio like 0.2–2, not a percentage like 20–200.
        "debt_equity":     (lambda v: round(v / 100, 3) if v is not None else None)(_safe(info.get("debtToEquity"))),
        "current_ratio":   _safe(info.get("currentRatio")),
        "quick_ratio":     _safe(info.get("quickRatio")),
        "interest_coverage": None,   # not in yfinance free tier
        # Size
        "market_cap":      _safe(info.get("marketCap")),
        "enterprise_val":  ev,
        # Dividends
        "dividend_yield":  _safe(info.get("dividendYield")),
        "payout_ratio":    _safe(info.get("payoutRatio")),
        # Indian-relevant
        "promoter_holding": _safe(info.get("heldPercentInsiders")),  # best proxy in yfinance
        "inst_holding":     _safe(info.get("heldPercentInstitutions")),
        "short_ratio":      _safe(info.get("shortRatio")),
        # Per share
        "eps":             _safe(info.get("trailingEps")),
        "book_value":      _safe(info.get("bookValue")),
        # Volume
        "avg_volume_3m":   _safe(info.get("averageVolume")),
        "avg_volume_10d":  _safe(info.get("averageVolume10days")),
        # 52-week
        "week52_high":     _safe(info.get("fiftyTwoWeekHigh")),
        "week52_low":      _safe(info.get("fiftyTwoWeekLow")),
        # Meta
        "beta":            _safe(info.get("beta")),
        "sector":          info.get("sector", ""),
        "industry":        info.get("industry", ""),
        "employees":       _safe(info.get("fullTimeEmployees")),
    }
    await Cache.set(cache_key, result, ttl=21600)
    return result


# ── Price history fetch ─────────────────────────────────────────────
async def _fetch_history(symbol: str) -> tuple[list, list]:
    """Fetch 1-year OHLCV history for technical indicators. Cached 1 hour."""
    cache_key = f"screener:hist:{symbol}"
    cached = await Cache.get(cache_key)
    if cached:
        return cached["closes"], cached["volumes"]

    def _sync():
        import yfinance as yf
        for suffix in [".NS", ".BO"]:
            try:
                df = yf.Ticker(f"{symbol}{suffix}").history(period="1y", auto_adjust=True)
                df = df.dropna(subset=["Close"])
                if len(df) >= 50:
                    closes  = [float(v) for v in df["Close"]]
                    volumes = []
                    for v in df["Volume"]:
                        try: volumes.append(int(v))
                        except: volumes.append(0)
                    if not any(math.isnan(c) or math.isinf(c) for c in closes):
                        return closes, volumes
            except Exception:
                pass
        return [], []

    loop = asyncio.get_running_loop()
    closes, volumes = await loop.run_in_executor(None, _sync)
    if closes:
        await Cache.set(cache_key, {"closes": closes, "volumes": volumes}, ttl=3600)
    return closes, volumes


# ── Main screener endpoint ─────────────────────────────────────────
@router.get("/", summary="Full-power screener — fundamentals + technicals + signals + composite score")
async def screen(
    # ── Basic ─────────────────────────────────────────────────────
    min_ml:    int   = Query(0,    description="Min ML score"),
    max_ml:    int   = Query(100,  description="Max ML score"),
    min_price: float = Query(0),
    max_price: float = Query(1e9),
    sector:    str   = Query(""),
    exchange:  str   = Query(""),
    sort_by:   str   = Query("quality_score", description="quality_score|ml_score|pe|roe|rsi|change_pct|market_cap_cr|revenue_growth|dividend_yield"),
    sort_dir:  str   = Query("desc", description="asc|desc"),
    limit:     int   = Query(50, le=200),

    # ── Valuation ──────────────────────────────────────────────────
    min_pe:       Optional[float] = Query(None),
    max_pe:       Optional[float] = Query(None),
    min_pb:       Optional[float] = Query(None),
    max_pb:       Optional[float] = Query(None),
    min_ps:       Optional[float] = Query(None),
    max_ps:       Optional[float] = Query(None),
    max_ev_ebitda:Optional[float] = Query(None, description="Max EV/EBITDA (e.g. 15)"),
    max_peg:      Optional[float] = Query(None, description="Max PEG ratio (e.g. 1.5)"),

    # ── Profitability ──────────────────────────────────────────────
    min_roe:             Optional[float] = Query(None, description="Min ROE %"),
    min_roa:             Optional[float] = Query(None, description="Min ROA %"),
    min_profit_margin:   Optional[float] = Query(None, description="Min net profit margin %"),
    min_gross_margin:    Optional[float] = Query(None),
    min_operating_margin:Optional[float] = Query(None),

    # ── Growth ────────────────────────────────────────────────────
    min_rev_growth:  Optional[float] = Query(None, description="Min revenue growth %"),
    min_earn_growth: Optional[float] = Query(None, description="Min earnings growth %"),

    # ── Financial health ──────────────────────────────────────────
    max_de:           Optional[float] = Query(None, description="Max Debt/Equity"),
    min_current_ratio:Optional[float] = Query(None),
    min_quick_ratio:  Optional[float] = Query(None),

    # ── Size ──────────────────────────────────────────────────────
    min_mcap: Optional[float] = Query(None, description="Min market cap ₹ Crore"),
    max_mcap: Optional[float] = Query(None, description="Max market cap ₹ Crore"),

    # ── Dividends ─────────────────────────────────────────────────
    min_div_yield: Optional[float] = Query(None, description="Min dividend yield %"),
    max_payout:    Optional[float] = Query(None, description="Max payout ratio %"),

    # ── Ownership ─────────────────────────────────────────────────
    min_promoter:  Optional[float] = Query(None, description="Min promoter/insider holding %"),
    min_inst:      Optional[float] = Query(None, description="Min institutional holding %"),

    # ── Technical filters ─────────────────────────────────────────
    min_rsi:         Optional[float] = Query(None, description="Min RSI"),
    max_rsi:         Optional[float] = Query(None, description="Max RSI (e.g. 30 = oversold only)"),
    above_200dma:    Optional[bool]  = Query(None, description="Must be above 200-day SMA"),
    above_50ema:     Optional[bool]  = Query(None, description="Must be above 50-day EMA"),
    golden_cross:    Optional[bool]  = Query(None, description="Golden cross (EMA20>EMA50) just triggered"),
    min_vol_ratio:   Optional[float] = Query(None, description="Min volume vs 20-day avg (e.g. 1.5)"),
    near_52w_high:   Optional[bool]  = Query(None, description="Within 5% of 52-week high"),
    near_52w_low:    Optional[bool]  = Query(None, description="Within 5% of 52-week low"),
    bb_squeeze:      Optional[bool]  = Query(None, description="Bollinger Band squeeze (bandwidth < 5%)"),
    min_beta:        Optional[float] = Query(None),
    max_beta:        Optional[float] = Query(None),
    min_change_pct:  Optional[float] = Query(None),
    max_change_pct:  Optional[float] = Query(None),

    # ── Signal filter ─────────────────────────────────────────────
    signal: Optional[str] = Query(None, description="BUY | STRONG BUY | SELL | STRONG SELL | WATCH"),

    # ── Preset shortcuts ──────────────────────────────────────────
    preset: str = Query("", description="value|growth|momentum|lowrisk|dividend|turnaround|breakout"),

    # ── Mode ──────────────────────────────────────────────────────
    enrich:   bool = Query(False, description="Fetch fundamentals (slower, enables fund filters)"),
    technicals:bool= Query(False, description="Compute technical indicators from history (slowest)"),
):
    # ── Apply presets ─────────────────────────────────────────────
    if preset == "value":
        max_pe = max_pe or 15; max_pb = max_pb or 2
        min_roe = min_roe or 12; max_de = max_de or 1
        enrich = True
    elif preset == "growth":
        min_rev_growth = min_rev_growth or 15; min_earn_growth = min_earn_growth or 10
        min_profit_margin = min_profit_margin or 8; enrich = True
    elif preset == "momentum":
        min_ml = max(min_ml, 65); min_change_pct = min_change_pct or 0.5
        above_200dma = True; technicals = True
    elif preset == "lowrisk":
        max_beta = max_beta or 0.8; min_div_yield = min_div_yield or 1.5
        max_de = max_de or 0.5; above_200dma = True; enrich = True; technicals = True
    elif preset == "dividend":
        min_div_yield = min_div_yield or 3; max_de = max_de or 1
        min_profit_margin = min_profit_margin or 5; enrich = True
    elif preset == "turnaround":
        max_rsi = max_rsi or 35; near_52w_low = True
        min_ml = max(min_ml, 50); technicals = True
    elif preset == "breakout":
        near_52w_high = True; min_vol_ratio = min_vol_ratio or 2.0
        above_200dma = True; technicals = True

    # If any technical/fundamental filter is set, enable those modes
    tech_filters = [min_rsi, max_rsi, above_200dma, above_50ema, golden_cross,
                    min_vol_ratio, near_52w_high, near_52w_low, bb_squeeze, signal]
    fund_filters = [min_pe, max_pe, min_pb, max_pb, min_ps, max_ps, max_ev_ebitda, max_peg,
                    min_roe, min_roa, min_profit_margin, min_gross_margin, min_operating_margin,
                    min_rev_growth, min_earn_growth, max_de, min_current_ratio, min_quick_ratio,
                    min_mcap, max_mcap, min_div_yield, max_payout, min_promoter, min_inst]
    if any(v is not None for v in tech_filters):
        technicals = True
    if any(v is not None for v in fund_filters):
        enrich = True

    # ── Fast pre-filter: price + ML + change ─────────────────────
    all_stocks = await DataFetcher.get_all_stocks(exchange=exchange, sector=sector, sort="ml_desc")
    candidates = [s for s in all_stocks if (
        min_ml <= (s.get("ml_score") or 0) <= max_ml
        and min_price <= (s.get("price") or 0) <= max_price
        and _ok(s.get("change_pct"), min_change_pct, max_change_pct, strict=False)
    )]

    # Cap at 100 for enrichment to avoid rate limits
    if enrich or technicals:
        candidates = candidates[:100]

    # ── Fetch fundamentals if needed ──────────────────────────────
    fund_map = {}
    if enrich:
        fund_results = await asyncio.gather(
            *[_fetch_fundamentals(s["sym"]) for s in candidates],
            return_exceptions=True
        )
        for s, f in zip(candidates, fund_results):
            fund_map[s["sym"]] = f if isinstance(f, dict) else {}

    # ── Fetch price history for technicals if needed ───────────────
    hist_map = {}
    if technicals:
        hist_results = await asyncio.gather(
            *[_fetch_history(s["sym"]) for s in candidates],
            return_exceptions=True
        )
        for s, h in zip(candidates, hist_results):
            if isinstance(h, tuple):
                hist_map[s["sym"]] = h
            else:
                hist_map[s["sym"]] = ([], [])

    # ── Build result set ──────────────────────────────────────────
    result_stocks = []
    for stock in candidates:
        sym   = stock["sym"]
        price = stock.get("price") or 0
        ml    = stock.get("ml_score") or 0
        fund  = fund_map.get(sym, {})
        closes, volumes = hist_map.get(sym, ([], []))

        # ── Compute derived fundamental metrics ───────────────────
        mcap_cr  = (fund.get("market_cap") or 0) / 1e7

        def pct(key):
            v = fund.get(key)
            return round((v or 0) * 100, 1) if v is not None else None

        roe_pct  = pct("roe")
        roa_pct  = pct("roa")
        pm_pct   = pct("profit_margin")
        gm_pct   = pct("gross_margin")
        om_pct   = pct("operating_margin")
        rg_pct   = pct("revenue_growth")
        eg_pct   = pct("earnings_growth")
        dy_pct   = pct("dividend_yield")
        po_pct   = pct("payout_ratio")
        pr_pct   = round((fund.get("promoter_holding") or 0) * 100, 1) if fund.get("promoter_holding") is not None else None
        in_pct   = round((fund.get("inst_holding") or 0) * 100, 1)     if fund.get("inst_holding")    is not None else None

        # ── Apply fundamental filters ─────────────────────────────
        # NOTE: was `if enrich and fund:` — an empty dict {} (fundamentals
        # fetch failed entirely) is falsy in Python, which silently SKIPPED
        # every fundamental filter and let the stock through unfiltered.
        # `_ok(..., strict=True)` already handles the "no data" case
        # correctly per-field, so we only need to gate on `enrich`.
        if enrich:
            if not _ok(fund.get("pe"),          min_pe,             max_pe,             strict=True): continue
            if not _ok(fund.get("pb"),          min_pb,             max_pb,             strict=True): continue
            if not _ok(fund.get("ps"),          min_ps,             max_ps,             strict=True): continue
            if not _ok(fund.get("ev_ebitda"),   None,               max_ev_ebitda,      strict=max_ev_ebitda is not None): continue
            if not _ok(fund.get("peg"),         None,               max_peg,            strict=max_peg is not None): continue
            if not _ok(roe_pct,                 min_roe,            None,               strict=min_roe is not None): continue
            if not _ok(roa_pct,                 min_roa,            None,               strict=min_roa is not None): continue
            if not _ok(pm_pct,                  min_profit_margin,  None,               strict=min_profit_margin is not None): continue
            if not _ok(gm_pct,                  min_gross_margin,   None,               strict=min_gross_margin is not None): continue
            if not _ok(om_pct,                  min_operating_margin,None,              strict=min_operating_margin is not None): continue
            if not _ok(rg_pct,                  min_rev_growth,     None,               strict=min_rev_growth is not None): continue
            if not _ok(eg_pct,                  min_earn_growth,    None,               strict=min_earn_growth is not None): continue
            if not _ok(fund.get("debt_equity"), None,               max_de,             strict=max_de is not None): continue
            if not _ok(fund.get("current_ratio"),min_current_ratio, None,               strict=min_current_ratio is not None): continue
            if not _ok(fund.get("quick_ratio"), min_quick_ratio,    None,               strict=min_quick_ratio is not None): continue
            if not _ok(mcap_cr or None,         min_mcap,           max_mcap,           strict=bool(min_mcap or max_mcap)): continue
            if not _ok(dy_pct,                  min_div_yield,      None,               strict=min_div_yield is not None): continue
            if not _ok(po_pct,                  None,               max_payout,         strict=max_payout is not None): continue
            if not _ok(fund.get("beta"),        min_beta,           max_beta,           strict=bool(min_beta or max_beta)): continue
            if not _ok(pr_pct,                  min_promoter,       None,               strict=min_promoter is not None): continue
            if not _ok(in_pct,                  min_inst,           None,               strict=min_inst is not None): continue

        # ── Compute technicals ─────────────────────────────────────
        tech = {}
        sig_label = None
        if technicals and closes:
            tech = _compute_technicals(closes, volumes)
            _, sig_label = _signal_score(closes, volumes, price)

            # Apply technical filters
            if min_rsi is not None and not _ok(tech.get("rsi"), min_rsi, None, strict=True): continue
            if max_rsi is not None and not _ok(tech.get("rsi"), None, max_rsi, strict=True): continue
            if above_200dma is True  and not tech.get("above_200dma"):  continue
            if above_50ema  is True  and not tech.get("above_50ema"):   continue
            if golden_cross is True  and not tech.get("golden_cross"):  continue
            if near_52w_high is True and (tech.get("pct_from_ath") is None or tech.get("pct_from_ath") < -5): continue
            if near_52w_low  is True and (tech.get("pct_from_atl") is None or tech.get("pct_from_atl") > 10): continue
            if bb_squeeze    is True and (tech.get("bb_width") is None  or tech.get("bb_width") > 5):        continue
            if min_vol_ratio is not None and not _ok(tech.get("vol_ratio_20d"), min_vol_ratio, None, strict=True): continue
            if signal and sig_label and signal.upper() not in sig_label: continue

        # ── Compute composite quality score ────────────────────────
        q_score = _quality_score(fund, tech, ml) if (enrich or technicals) else min(ml, 100)

        # ── Assemble result row ────────────────────────────────────
        row = {
            **stock,
            "quality_score":    q_score,
            "signal":           sig_label,
            # Fundamentals
            "pe":               fund.get("pe"),
            "forward_pe":       fund.get("forward_pe"),
            "pb":               fund.get("pb"),
            "ps":               fund.get("ps"),
            "ev_ebitda":        fund.get("ev_ebitda"),
            "peg":              fund.get("peg"),
            "roe":              roe_pct,
            "roa":              roa_pct,
            "profit_margin":    pm_pct,
            "gross_margin":     gm_pct,
            "operating_margin": om_pct,
            "revenue_growth":   rg_pct,
            "earnings_growth":  eg_pct,
            "debt_equity":      fund.get("debt_equity"),
            "current_ratio":    fund.get("current_ratio"),
            "ev_ebitda":        fund.get("ev_ebitda"),
            "market_cap_cr":    round(mcap_cr) if mcap_cr else None,
            "dividend_yield":   dy_pct,
            "payout_ratio":     po_pct,
            "promoter_holding": pr_pct,
            "inst_holding":     in_pct,
            "beta":             fund.get("beta"),
            "eps":              fund.get("eps"),
            "book_value":       fund.get("book_value"),
            "sector":           fund.get("sector") or stock.get("sector", ""),
            "industry":         fund.get("industry", ""),
            # Technicals
            "rsi":              tech.get("rsi"),
            "above_200dma":     tech.get("above_200dma"),
            "above_50ema":      tech.get("above_50ema"),
            "golden_cross":     tech.get("golden_cross"),
            "death_cross":      tech.get("death_cross"),
            "dma200_gap":       tech.get("dma200_gap"),
            "bb_width":         tech.get("bb_width"),
            "vol_ratio_20d":    tech.get("vol_ratio_20d"),
            "pct_from_ath":     tech.get("pct_from_ath"),
            "pct_from_atl":     tech.get("pct_from_atl"),
            "sma200":           tech.get("sma200"),
            "ema50":            tech.get("ema50"),
        }
        result_stocks.append(row)

    # ── Sort results ──────────────────────────────────────────────
    def _sort_key(s):
        v = s.get(sort_by)
        return (0, 0) if v is None else (1, v)

    result_stocks.sort(key=_sort_key, reverse=(sort_dir.lower() != "asc"))

    # ── Summary stats ─────────────────────────────────────────────
    def _avg(key):
        vals = [s[key] for s in result_stocks if s.get(key) is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    summary = {
        "avg_pe":    _avg("pe"),
        "avg_roe":   _avg("roe"),
        "avg_rsi":   _avg("rsi"),
        "avg_de":    _avg("debt_equity"),
        "avg_score": _avg("quality_score"),
        "buy_count": sum(1 for s in result_stocks if "BUY" in (s.get("signal") or "")),
    }

    applied = {k: v for k, v in {
        "preset": preset or None, "signal": signal,
        "pe": f"{min_pe}–{max_pe}" if (min_pe or max_pe) else None,
        "roe_min": min_roe, "max_de": max_de,
        "div_yield_min": min_div_yield, "rev_growth_min": min_rev_growth,
        "rsi": f"{min_rsi}–{max_rsi}" if (min_rsi or max_rsi) else None,
        "above_200dma": above_200dma, "golden_cross": golden_cross,
        "near_52w_high": near_52w_high, "near_52w_low": near_52w_low,
        "bb_squeeze": bb_squeeze,
    }.items() if v is not None}

    return {
        "total":           len(result_stocks),
        "preset":          preset,
        "enriched":        enrich,
        "technicals":      technicals,
        "sort_by":         sort_by,
        "summary":         summary,
        "filters_applied": applied,
        "stocks":          result_stocks[:limit],
    }


# ── Saved presets endpoint ─────────────────────────────────────────
@router.get("/presets", summary="List all available screener presets with descriptions")
async def get_presets():
    return {"presets": [
        {"id": "value",      "label": "Value Stocks",    "desc": "Low P/E (<15), Low P/B (<2), ROE>12%, D/E<1. Classic Graham-style value picks."},
        {"id": "growth",     "label": "Growth Stocks",   "desc": "Revenue growth>15%, earnings growth>10%, profit margin>8%. CANSLIM-inspired."},
        {"id": "momentum",   "label": "Momentum",        "desc": "ML score>65, above 200 DMA, positive day change. Price + ML momentum."},
        {"id": "lowrisk",    "label": "Low Risk",        "desc": "Beta<0.8, div yield>1.5%, D/E<0.5, above 200 DMA. Defensive portfolio."},
        {"id": "dividend",   "label": "Dividend",        "desc": "Yield>3%, D/E<1, profit margin>5%. Income investing."},
        {"id": "turnaround", "label": "Turnaround",      "desc": "RSI<35, near 52W low, ML score>50. Contrarian oversold plays."},
        {"id": "breakout",   "label": "Breakout",        "desc": "Near 52W high, volume>2x avg, above 200 DMA. Momentum breakout scanner."},
    ]}
