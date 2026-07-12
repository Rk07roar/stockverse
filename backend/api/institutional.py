"""
StockVest — api/institutional.py
Real institutional-grade market data:
  - NSE sector index performance (IT, Bank, Pharma, Auto, FMCG, Metal, Energy, Infra)
  - India VIX fear gauge (real ^INDIAVIX value)
  - Fear & Greed index derived from India VIX
  - Nifty50 30-day trend
  - Top gainers / losers from real Nifty100 quotes
  - FII/DII daily flow data from NSE's FREE public archive CSV
    Source: https://archives.nseindia.com/content/fo/fno_participant_oi_and_vol.csv
    Updated daily after market close (5–6 PM IST). No API key required.
"""
import asyncio
import logging
import io
from fastapi import APIRouter, Query

from data.cache import Cache
from data.fetcher import DataFetcher

logger = logging.getLogger(__name__)
router = APIRouter()

SECTOR_TICKERS = {
    "IT":     "^CNXIT",
    "Bank":   "^NSEBANK",
    "Pharma": "^CNXPHARMA",
    "Auto":   "^CNXAUTO",
    "FMCG":   "^CNXFMCG",
    "Metal":  "^CNXMETAL",
    "Energy": "^CNXENERGY",
    "Infra":  "^CNXINFRA",
}


def _fetch_sector_data_sync():
    """
    Fetch real NSE sector index OHLCV from yfinance.
    Returns sector performance, India VIX, and Nifty50 trend — all real values.
    """
    try:
        import yfinance as yf
        import pandas as pd
        import math

        tickers = list(SECTOR_TICKERS.values()) + ["^NSEI", "^INDIAVIX"]
        raw = yf.download(tickers, period="30d", interval="1d",
                          group_by="ticker", auto_adjust=True, progress=False)
        if raw.empty:
            return None

        sectors = {}
        for name, tk in SECTOR_TICKERS.items():
            try:
                df = (raw[tk] if isinstance(raw.columns, pd.MultiIndex) and
                      tk in raw.columns.get_level_values(0) else raw).dropna(how="all")
                if df.empty or len(df) < 2:
                    continue
                latest = float(df["Close"].iloc[-1])
                prev   = float(df["Close"].iloc[-2])
                w_ago  = float(df["Close"].iloc[max(0, len(df) - 6)])
                m_ago  = float(df["Close"].iloc[0])
                if math.isnan(latest):
                    continue
                sectors[name] = {
                    "value":  round(latest, 2),
                    "chg_1d": round((latest / prev  - 1) * 100, 2),
                    "chg_1w": round((latest / w_ago - 1) * 100, 2),
                    "chg_1m": round((latest / m_ago - 1) * 100, 2),
                }
            except Exception:
                pass

        # India VIX — real value from ^INDIAVIX
        vix_val = None
        try:
            if isinstance(raw.columns, pd.MultiIndex) and "^INDIAVIX" in raw.columns.get_level_values(0):
                df = raw["^INDIAVIX"].dropna(how="all")
                if not df.empty:
                    v = float(df["Close"].iloc[-1])
                    if not math.isnan(v):
                        vix_val = round(v, 2)
        except Exception:
            pass

        # Nifty50 — real 30-day trend
        nifty_trend = None
        try:
            if isinstance(raw.columns, pd.MultiIndex) and "^NSEI" in raw.columns.get_level_values(0):
                df = raw["^NSEI"].dropna(how="all")
                if not df.empty and len(df) >= 2:
                    latest = float(df["Close"].iloc[-1])
                    m_ago  = float(df["Close"].iloc[0])
                    if not math.isnan(latest):
                        nifty_trend = {
                            "value":     round(latest, 2),
                            "chg_1m":    round((latest / m_ago - 1) * 100, 2),
                            "direction": "bullish" if latest > m_ago else "bearish",
                            # 30-day close series for charting
                            "dates":  [str(d.date()) for d in df.index],
                            "closes": [round(float(v), 2) for v in df["Close"]],
                        }
        except Exception:
            pass

        return {"sectors": sectors, "vix": vix_val, "nifty": nifty_trend}

    except Exception as e:
        logger.warning(f"Sector data fetch failed: {e}")
        return None


def _vix_to_fear_greed(vix: float) -> tuple:
    """
    Convert India VIX to a 0-100 Fear & Greed score using standard VIX thresholds.
    VIX < 12 = Extreme Greed (low fear), VIX > 25 = Extreme Fear (high fear).
    Returns (score, label).
    """
    if vix < 11:
        score = min(98, int(95 - vix))
    elif vix < 14:
        score = int(85 - (vix - 11) * 5)
    elif vix < 17:
        score = int(70 - (vix - 14) * 7)
    elif vix < 20:
        score = int(49 - (vix - 17) * 6)
    elif vix < 25:
        score = int(31 - (vix - 20) * 3)
    else:
        score = max(5, int(16 - (vix - 25) * 1.5))

    score = max(5, min(98, score))
    label = (
        "Extreme Greed" if score >= 75 else
        "Greed"         if score >= 60 else
        "Neutral"       if score >= 40 else
        "Fear"          if score >= 25 else
        "Extreme Fear"
    )
    return score, label


def _sector_label(chg_1m: float) -> tuple:
    """Label a sector as Bullish / Neutral / Bearish from its real 1-month return."""
    if chg_1m > 3:
        return "Bullish", "#00d4aa"
    elif chg_1m < -3:
        return "Bearish", "#ff4757"
    else:
        return "Neutral", "#ffd32a"


@router.get("/", summary="Institutional market dashboard — sectors, VIX, Fear & Greed")
async def get_institutional(refresh: bool = Query(False)):
    cache_key = "institutional:dashboard"
    if not refresh:
        cached = await Cache.get(cache_key)
        if cached:
            return cached

    loop = asyncio.get_event_loop()
    sector_data = await loop.run_in_executor(None, _fetch_sector_data_sync)

    stocks  = await DataFetcher.get_all_stocks(sort="chg_desc")
    real    = [s for s in stocks if s.get("real_data")]
    gainers = real[:5]
    losers  = list(reversed(real))[:5]

    result = {
        "sector_performance": sector_data.get("sectors", {}) if sector_data else {},
        "india_vix":          sector_data.get("vix")         if sector_data else None,
        "nifty_trend":        sector_data.get("nifty")       if sector_data else None,
        "top_gainers":        gainers,
        "top_losers":         losers,
    }

    await Cache.set(cache_key, result, ttl=600)
    return result


@router.get("/sectors", summary="NSE sector index performance — real values")
async def get_sectors():
    cached = await Cache.get("institutional:sectors")
    if cached:
        return cached
    loop   = asyncio.get_event_loop()
    data   = await loop.run_in_executor(None, _fetch_sector_data_sync)
    result = data or {"sectors": {}, "vix": None, "nifty": None}
    await Cache.set("institutional:sectors", result, ttl=600)
    return result


@router.get("/live", summary="Live institutional dashboard — real VIX, sector indices, Fear & Greed — 2-min cache")
async def get_live_institutional(refresh: bool = Query(False)):
    """
    All values are derived from real yfinance data:
      • Fear & Greed   — computed from real India VIX level
      • Sector labels  — Bullish/Neutral/Bearish from actual 1-month index return
      • Nifty 30-day trend — real daily close series

    FII/DII flow data is NOT included. Real SEBI flow data requires a paid feed.
    """
    cache_key = "institutional:live"
    if not refresh:
        cached = await Cache.get(cache_key)
        if cached:
            return cached

    loop        = asyncio.get_event_loop()
    sector_data = await loop.run_in_executor(None, _fetch_sector_data_sync)

    # ── Fear & Greed from real India VIX ─────────────────────
    vix = (sector_data or {}).get("vix")
    if vix and vix > 0:
        fg, fg_label = _vix_to_fear_greed(vix)
    else:
        fg, fg_label = None, "Unavailable"

    # ── Sector performance with Bullish/Neutral/Bearish labels ─
    sectors_raw = (sector_data or {}).get("sectors", {})
    sectors_out = {}
    for name, d in sectors_raw.items():
        label, col = _sector_label(d.get("chg_1m", 0))
        sectors_out[name] = {**d, "label": label, "color": col}

    # ── FII/DII from NSE free archive ────────────────────────────
    fii_dii = await _fetch_fii_dii()

    result = {
        "fear_greed":       fg,
        "fear_greed_label": fg_label,
        "vix":              vix,
        "nifty":            (sector_data or {}).get("nifty"),
        "sectors":          sectors_out,
        "fii_dii":          fii_dii,
    }
    await Cache.set(cache_key, result, ttl=120)
    return result


# ── FII/DII from NSE's free public CSV ───────────────────────────

def _parse_fii_dii_csv(text: str) -> dict:
    """
    Parse NSE's participant OI/volume CSV.
    Columns: Client Type, Future Long, Future Short, Option Call Long,
             Option Call Short, Option Put Long, Option Put Short, Total Long, Total Short
    We use Total Long - Total Short as net position proxy.
    """
    try:
        import csv
        reader = csv.DictReader(io.StringIO(text))
        result = {}
        for row in reader:
            client = (row.get("Client Type") or row.get("CLIENT TYPE") or "").strip()
            if not client:
                continue
            def _n(k):
                for key in row:
                    if k.lower() in key.lower():
                        try: return int(str(row[key]).replace(",", "").strip() or "0")
                        except: return 0
                return 0
            total_long  = _n("Total Long")
            total_short = _n("Total Short")
            net         = total_long - total_short
            if "FII" in client.upper() or "FPI" in client.upper():
                result["FII"] = {"long": total_long, "short": total_short, "net": net,
                                 "label": "Bullish" if net > 0 else "Bearish",
                                 "color": "#00d4aa" if net > 0 else "#ff4757"}
            elif "DII" in client.upper():
                result["DII"] = {"long": total_long, "short": total_short, "net": net,
                                 "label": "Bullish" if net > 0 else "Bearish",
                                 "color": "#00d4aa" if net > 0 else "#ff4757"}
            elif "PRO" in client.upper():
                result["PRO"] = {"long": total_long, "short": total_short, "net": net,
                                 "label": "Bullish" if net > 0 else "Bearish",
                                 "color": "#00d4aa" if net > 0 else "#ff4757"}
            elif "CLIENT" in client.upper() or "RETAIL" in client.upper():
                result["RETAIL"] = {"long": total_long, "short": total_short, "net": net,
                                    "label": "Bullish" if net > 0 else "Bearish",
                                    "color": "#00d4aa" if net > 0 else "#ff4757"}
        return result
    except Exception as e:
        logger.warning(f"FII/DII CSV parse failed: {e}")
        return {}


async def _fetch_fii_dii() -> dict:
    """
    Fetch FII/DII participant data from NSE free archive.
    This CSV is updated daily after market close — no API key, no subscription.
    """
    cache_key = "institutional:fii_dii"
    cached    = await Cache.get(cache_key)
    if cached:
        return cached

    url = "https://archives.nseindia.com/content/fo/fno_participant_oi_and_vol.csv"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.nseindia.com/",
    }
    try:
        import httpx
        async with httpx.AsyncClient(headers=headers, timeout=15, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = _parse_fii_dii_csv(resp.text)
            if data:
                await Cache.set(cache_key, data, ttl=3600)  # cache 1 hour
            return data
    except Exception as e:
        logger.warning(f"FII/DII fetch failed: {e}")
        return {}


@router.get("/fii-dii", summary="Real FII/DII net positions from NSE free archive CSV")
async def get_fii_dii():
    """
    Returns FII, DII, PRO, and Retail participant net positions.
    Data source: NSE archives (free, no API key).
    Updated daily after market close (~5-6 PM IST).
    """
    data = await _fetch_fii_dii()
    if not data:
        return {"error": "FII/DII data unavailable — NSE may be updating. Try after 6 PM IST.",
                "FII": None, "DII": None}
    return data
