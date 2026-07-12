"""
StockVest — api/options.py
NSE Options Chain: live OI, Volume, IV per strike.
PCR (Put-Call Ratio), Max Pain, OI Buildup/Unwinding signals.
All data from NSE's FREE public API — no paid subscription needed.
"""
import asyncio
import logging
import httpx
from fastapi import APIRouter, Query
from data.cache import Cache

logger = logging.getLogger(__name__)
router = APIRouter()

# NSE blocks plain requests — these headers mimic a real browser
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
    "Connection": "keep-alive",
}

NSE_HOME     = "https://www.nseindia.com"
NSE_INDEX_OC = "https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
NSE_EQ_OC    = "https://www.nseindia.com/api/option-chain-equities?symbol={symbol}"


async def _fetch_nse_options(symbol: str, is_index: bool = True) -> dict | None:
    """
    Fetch options chain from NSE with session cookie.
    Three-step (mirrors the proven nse_fetcher.py approach):
      1. Hit homepage  → Akamai sets base cookies
      2. Wait 1.5s     → bot-detection timing requirement
      3. Fetch chain   → now accepted as a real browser session
    """
    try:
        async with httpx.AsyncClient(
            headers=NSE_HEADERS, follow_redirects=True, timeout=25
        ) as client:
            # Step 1: homepage hit — Akamai/Cloudflare sets session cookies
            await client.get(NSE_HOME)
            # Step 2: brief delay (NSE rejects requests that come too fast)
            await asyncio.sleep(1.5)
            # Step 3: market-data warmup — sets additional session cookies
            await client.get(
                "https://www.nseindia.com/market-data/live-equity-market",
                headers={**NSE_HEADERS, "Referer": "https://www.nseindia.com/"}
            )
            await asyncio.sleep(1.0)
            # Step 4: fetch the actual options chain
            url = (NSE_INDEX_OC if is_index else NSE_EQ_OC).format(symbol=symbol)
            resp = await client.get(
                url,
                headers={**NSE_HEADERS, "Referer": "https://www.nseindia.com/option-chain"}
            )
            resp.raise_for_status()
            data = resp.json()
            # NSE sometimes returns an HTML error page — check for expected keys
            if not isinstance(data, dict) or "records" not in data:
                logger.warning(f"NSE returned unexpected response for {symbol}: {str(data)[:120]}")
                return None
            return data
    except Exception as e:
        logger.warning(f"NSE options fetch failed for {symbol}: {e}")
        return None


def _calc_max_pain(strikes: list) -> float:
    """
    Max Pain = strike where total dollar payout to option BUYERS is minimum.
    Writers (sellers) profit most at this strike — market often gravitates here at expiry.
    """
    if not strikes:
        return 0
    pain = {}
    for s in strikes:
        sp = s["strike"]
        total = 0
        for k in strikes:
            kp = k["strike"]
            total += max(0, sp - kp) * k["ce_oi"]   # CE buyer loss
            total += max(0, kp - sp) * k["pe_oi"]   # PE buyer loss
        pain[sp] = total
    return min(pain, key=pain.get) if pain else 0


def _oi_buildup_signals(strikes: list, underlying: float) -> list:
    """
    Detect OI buildup (new positions) and unwinding (closing) near ATM.
    Buildup at CE = resistance. Buildup at PE = support.
    """
    signals = []
    if not underlying or not strikes:
        return signals

    # Only look at strikes within 5% of spot
    near = [s for s in strikes if abs(s["strike"] - underlying) / underlying < 0.05]

    for s in near:
        strike = s["strike"]
        label = ("ATM" if abs(strike - underlying) / underlying < 0.005
                 else ("ITM" if strike < underlying else "OTM"))

        if s["ce_chg_oi"] > 50_000:
            signals.append({"strike": strike, "type": "CE Buildup",   "label": label,
                            "chg_oi": s["ce_chg_oi"], "color": "#ff4757",
                            "note": f"Bears adding CE shorts at {strike} — resistance forming"})
        elif s["ce_chg_oi"] < -50_000:
            signals.append({"strike": strike, "type": "CE Unwinding", "label": label,
                            "chg_oi": s["ce_chg_oi"], "color": "#00d4aa",
                            "note": f"CE shorts covering at {strike} — resistance weakening"})
        if s["pe_chg_oi"] > 50_000:
            signals.append({"strike": strike, "type": "PE Buildup",   "label": label,
                            "chg_oi": s["pe_chg_oi"], "color": "#00d4aa",
                            "note": f"Bulls adding PE at {strike} — strong support forming"})
        elif s["pe_chg_oi"] < -50_000:
            signals.append({"strike": strike, "type": "PE Unwinding", "label": label,
                            "chg_oi": s["pe_chg_oi"], "color": "#ff4757",
                            "note": f"PE longs exiting at {strike} — support weakening"})

    return signals[:6]


def _parse_options_chain(raw: dict, expiry: str = None) -> dict:
    """Parse raw NSE JSON into a clean structured format."""
    if not raw or "records" not in raw:
        return {}

    records      = raw["records"]
    expiry_dates = records.get("expiryDates", [])
    if not expiry_dates:
        return {}

    target_expiry = expiry or expiry_dates[0]
    underlying    = float(records.get("underlyingValue", 0))

    strikes_map = {}
    for item in records.get("data", []):
        if item.get("expiryDate") != target_expiry:
            continue
        sp = item.get("strikePrice", 0)
        ce = item.get("CE", {})
        pe = item.get("PE", {})
        strikes_map[sp] = {
            "strike":    sp,
            "ce_oi":     ce.get("openInterest", 0),
            "ce_chg_oi": ce.get("changeinOpenInterest", 0),
            "ce_volume": ce.get("totalTradedVolume", 0),
            "ce_iv":     round(ce.get("impliedVolatility", 0), 2),
            "ce_ltp":    ce.get("lastPrice", 0),
            "ce_bid":    ce.get("bidprice", 0),
            "ce_ask":    ce.get("askPrice", 0),
            "pe_oi":     pe.get("openInterest", 0),
            "pe_chg_oi": pe.get("changeinOpenInterest", 0),
            "pe_volume": pe.get("totalTradedVolume", 0),
            "pe_iv":     round(pe.get("impliedVolatility", 0), 2),
            "pe_ltp":    pe.get("lastPrice", 0),
            "pe_bid":    pe.get("bidprice", 0),
            "pe_ask":    pe.get("askPrice", 0),
        }

    sorted_strikes = sorted(strikes_map.values(), key=lambda x: x["strike"])

    total_ce_oi = sum(s["ce_oi"] for s in sorted_strikes)
    total_pe_oi = sum(s["pe_oi"] for s in sorted_strikes)
    pcr         = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 0
    pcr_label   = "Bullish" if pcr > 1.2 else ("Bearish" if pcr < 0.8 else "Neutral")
    max_pain    = _calc_max_pain(sorted_strikes)
    oi_signals  = _oi_buildup_signals(sorted_strikes, underlying)

    # Nearest ATM strike index for frontend to highlight
    atm_idx = min(range(len(sorted_strikes)),
                  key=lambda i: abs(sorted_strikes[i]["strike"] - underlying)) if sorted_strikes else 0

    return {
        "underlying":   underlying,
        "expiry":       target_expiry,
        "expiry_dates": expiry_dates[:10],
        "pcr":          pcr,
        "pcr_label":    pcr_label,
        "max_pain":     max_pain,
        "total_ce_oi":  total_ce_oi,
        "total_pe_oi":  total_pe_oi,
        "atm_idx":      atm_idx,
        "strikes":      sorted_strikes,
        "oi_signals":   oi_signals,
    }


@router.get("/chain", summary="NSE Options Chain — OI, Volume, IV per strike (free NSE data)")
async def get_options_chain(
    symbol: str = Query("NIFTY", description="NIFTY, BANKNIFTY, FINNIFTY, or any NSE equity symbol"),
    expiry: str = Query("", description="Expiry date string, blank = nearest expiry"),
):
    symbol = symbol.upper().strip()
    cache_key = f"options:chain:{symbol}:{expiry}"
    cached = await Cache.get(cache_key)
    if cached:
        return cached

    is_index = symbol in {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"}
    raw = await _fetch_nse_options(symbol, is_index)
    if not raw:
        return {"error": "NSE data unavailable — market may be closed or NSE is rate limiting",
                "strikes": [], "pcr": 0, "max_pain": 0, "oi_signals": []}

    result = _parse_options_chain(raw, expiry or None)
    if result and result.get("strikes"):
        await Cache.set(cache_key, result, ttl=60)   # 1-min cache — options move fast
    return result


@router.get("/pcr", summary="Quick PCR snapshot for NIFTY and BANKNIFTY")
async def get_pcr():
    """Dashboard widget: PCR + Max Pain for both indices."""
    cache_key = "options:pcr_snapshot"
    cached = await Cache.get(cache_key)
    if cached:
        return cached

    results = {}
    for sym in ["NIFTY", "BANKNIFTY"]:
        raw = await _fetch_nse_options(sym, is_index=True)
        if raw:
            parsed = _parse_options_chain(raw)
            results[sym] = {
                "pcr":        parsed.get("pcr", 0),
                "pcr_label":  parsed.get("pcr_label", "N/A"),
                "max_pain":   parsed.get("max_pain", 0),
                "underlying": parsed.get("underlying", 0),
                "expiry":     parsed.get("expiry", ""),
            }

    await Cache.set(cache_key, results, ttl=120)
    return results
