"""
StockVest — data/fetcher.py
DataFetcher: loads all NSE/BSE stocks, manages in-memory price cache.

Bulk price sources (NO rate limits):
  1. NSE Bhav Copy   — all ~1800 NSE EQ stocks, one ZIP download
  2. NSE Live Market — real-time for ~900 Nifty index stocks (session-based)
  3. BSE Bhav Copy   — all BSE EQ stocks, one ZIP download
  4. Disk cache      — price_cache.json survives restarts (instant load)

yfinance is kept ONLY for:
  - Individual on-demand get_quote() when a stock isn't in bhav copy yet
  - Price history (get_history)
  - Market indices (get_indices)
"""
import asyncio
import json
import math
import os
import logging
from typing import Optional, List, Dict

import httpx
from data.cache import Cache
from data.nse_fetcher import (
    fetch_nse_bhav_copy,
    fetch_nse_live_quotes,
    fetch_bse_bhav_copy,
    fetch_bse_prices_by_group,
    fetch_nse_equity_list,
    fetch_bse_equity_list,
    fetch_bse_prices_per_stock,
    is_market_open,
)

logger = logging.getLogger(__name__)

ALL_STOCKS_URL   = "https://analyst.indianapi.in/static/all_stocks.json"
PRICE_CACHE_PATH   = os.path.join(os.path.dirname(__file__), "price_cache.json")
BSE_NAME_MAP_PATH  = os.path.join(os.path.dirname(__file__), "bse_name_map.json")

# ── Nifty 100 symbols ─────────────────────────────────────────
NIFTY100_NSE = [
    "RELIANCE","TCS","HDFCBANK","ICICIBANK","INFY","HINDUNILVR","ITC","KOTAKBANK",
    "AXISBANK","LT","SBIN","BAJFINANCE","BHARTIARTL","ASIANPAINT","MARUTI","TITAN",
    "SUNPHARMA","ULTRACEMCO","WIPRO","NTPC","ONGC","POWERGRID","M&M","TATAMOTORS",
    "NESTLEIND","TECHM","HCLTECH","BPCL","COALINDIA","HEROMOTOCO","DIVISLAB",
    "DRREDDY","EICHERMOT","CIPLA","ADANIENT","JSWSTEEL","GRASIM","TATASTEEL",
    "HINDALCO","BAJAJFINSV","SBILIFE","HDFCLIFE","APOLLOHOSP","ADANIPORTS",
    "INDUSINDBK","BRITANNIA","SHREECEM","TATACONSUM","UPL","ETERNAL","PERSISTENT",
    "POLYCAB","CHOLAFIN","MPHASIS","COFORGE","LTTS","TATAELXSI","HAVELLS","DIXON",
    "MOTHERSON","BHARATFORG","BOSCHLTD","BAJAJ-AUTO","TVSMOTOR","ASHOKLEY",
    "ESCORTS","APOLLOTYRE","BALKRISIND","CEAT","MRF","EXIDEIND","ARE&M",
    "SONACOMS","JSWENERGY","TATAPOWER","TORNTPOWER","ADANIPOWER","ADANIGREEN",
    "NHPC","VEDL","SAIL","NMDC","HINDZINC","NATIONALUM","JINDALSTEL","JSWINFRA",
    "DLF","GODREJPROP","OBEROIRLTY","PRESTIGE","IRCTC","CONCOR","INDIGO",
    "DEEPAKNTR","NAVINFLUOR","AARTIIND","SRF","PIIND","COROMANDEL","ACC",
    "AMBUJACEM","DALBHARAT","RAMCOCEM",
]

# ── Extra stocks to validate via yfinance (known bad NSE data / penny stocks) ──
_YF_EXTRA_VALIDATE = [
    "IDEA","YESBANK","SUZLON","JPPOWER","JPASSOCIAT","RPOWER","DHFL",
    "ZOMATO","NYKAA","PAYTM","POLICYBZR","DELHIVERY","MAPMYINDIA",
    "VODAFONE","RTNPOWER","IRFC","RVNL","HAL","BEL","COCHINSHIP",
]

SECTOR_KEYWORDS = {
    'IT':       ['software','tech','infotech','digital','cloud','systems','computer','tcs','wipro','infosys','hcl','persistent','mphasis','mindtree','hexaware','coforge','niit'],
    'Bank':     ['bank','banking','sbi','hdfc','icici','kotak','axis','federal','rbl','idfc','indusin','pnb','canara','uco','central','union','indian bank'],
    'Pharma':   ['pharma','drug','medicine','biotech','health','hospital','diagnostic','lab','medic','cipla','lupin','drreddy','alkem','torrent','aurobindo','glenmark'],
    'Auto':     ['motor','automobile','automotive','vehicle','tyre','auto','maruti','mahindra','bajaj auto','hero','tvs','ashok','eicher','exide','minda','motherson'],
    'FMCG':     ['consumer','food','beverage','fmcg','household','nestle','hindustan unilever','itc','dabur','marico','colgate','britannia','godrej','emami','patanjali','zomato','eternal','swiggy'],
    'Energy':   ['energy','oil','gas','petroleum','power','electric','ntpc','ongc','bpcl','hpcl','gail','adani power','tata power','jsw energy','torrent power','coal india'],
    'Metal':    ['steel','metal','aluminium','copper','zinc','iron','ore','mining','jindal','hindalco','vedanta','sail','national aluminium','nmdc','moil'],
    'Telecom':  ['telecom','communication','network','mobile','airtel','vodafone','idea','route mobile','gtpl'],
    'Infra':    ['infra','construction','cement','realty','real estate','builder','highway','port','airport','larsen','acc','ultratech','shree cement','dalmia'],
    'Chemical': ['chemical','fertiliser','pesticide','agrochemical','specialty','paint','coromandel','deepak','navin fluorine','balaji amines','aarti','srf','pi industries'],
    'Textile':  ['textile','garment','fabric','yarn','apparel','cotton','polyester','raymond','vardhman','welspun','trident','arvind'],
}

def _get_sector(name: str, sym: str = "") -> str:
    txt = (name + " " + sym).lower()
    for sector, kws in SECTOR_KEYWORDS.items():
        if any(k in txt for k in kws):
            return sector
    return "Other"


# Negative cache: symbols that returned no data from yfinance (avoids hammering Yahoo)
_yf_miss_cache: Dict[str, float] = {}   # {sym: timestamp_when_missed}
_YF_MISS_TTL = 2 * 3600               # don't retry for 2 hours

# ── Price sanity check ────────────────────────────────────────
_MAX_PRICE_MOVE = 0.40   # reject if new price deviates >40% from last known
_MAX_ABSOLUTE_PRICE = 200_000  # no Indian equity trades above ₹2 lakh

def _sanity_filter(new_quotes: Dict[str, dict], existing: Dict[str, dict]) -> Dict[str, dict]:
    """
    Return only the quotes whose price passes sanity checks:
      1. Price must be > 0 and <= _MAX_ABSOLUTE_PRICE
      2. If we have a prior price, the change must be <= _MAX_PRICE_MOVE (40%)
    Logs every rejection so we can audit bad data.
    """
    clean: Dict[str, dict] = {}
    for sym, q in new_quotes.items():
        price = q.get("price", 0)
        # Basic bounds
        if price <= 0 or math.isnan(price) or price > _MAX_ABSOLUTE_PRICE:
            logger.warning(f"PRICE REJECTED {sym}: price={price} out of bounds (0, {_MAX_ABSOLUTE_PRICE}]")
            continue
        # Change-rate check against last known price
        old = existing.get(sym)
        if old:
            old_price = old.get("price", 0)
            if old_price > 0:
                change = abs(price - old_price) / old_price
                if change > _MAX_PRICE_MOVE:
                    logger.warning(
                        f"PRICE REJECTED {sym}: new={price} old={old_price} "
                        f"change={change*100:.1f}% exceeds {_MAX_PRICE_MOVE*100:.0f}% limit"
                    )
                    continue
        clean[sym] = q
    return clean


def _fetch_yfinance_batch(symbols: list) -> Dict[str, dict]:
    """
    Batch-download closing prices from yfinance for a list of NSE symbols.
    Uses yf.download() which fetches all tickers in one HTTP call — fast (~10-20s for 100 stocks).
    Returns {SYMBOL: {price, change_pct, open, high, low, volume, ...}}
    Used as a price baseline so sanity filter can catch bad NSE live data.
    """
    import io as _io, sys as _sys
    result: Dict[str, dict] = {}
    if not symbols:
        return result
    try:
        import yfinance as yf
        import pandas as pd

        tickers = [f"{s}.NS" for s in symbols]
        _orig = _sys.stderr
        _sys.stderr = _io.StringIO()
        try:
            raw = yf.download(
                tickers, period="2d", interval="1d",
                auto_adjust=True, progress=False,
                group_by="ticker", threads=True,
            )
        finally:
            _sys.stderr = _orig

        if raw is None or raw.empty:
            return result

        for sym in symbols:
            ticker = f"{sym}.NS"
            try:
                # Handle both multi-index (multiple tickers) and flat (single ticker)
                if isinstance(raw.columns, pd.MultiIndex):
                    if ticker not in raw.columns.get_level_values(0):
                        continue
                    df = raw[ticker].dropna(how="all")
                else:
                    df = raw.dropna(how="all")

                if df.empty:
                    continue

                price = float(df["Close"].iloc[-1])
                prev  = float(df["Close"].iloc[-2]) if len(df) > 1 else price
                if price <= 0 or math.isnan(price):
                    continue

                result[sym] = {
                    "price":      round(price, 2),
                    "change_pct": round((price - prev) / prev * 100, 2) if prev else 0,
                    "open":       round(float(df["Open"].iloc[-1] or price), 2),
                    "high":       round(float(df["High"].iloc[-1] or price), 2),
                    "low":        round(float(df["Low"].iloc[-1]  or price), 2),
                    "volume":     int(df["Volume"].iloc[-1] or 0),
                    "mcap":       None,
                    "pe":         None,
                }
            except Exception:
                continue

        logger.info(f"yfinance batch baseline: {len(result)}/{len(symbols)} stocks loaded")
    except Exception as e:
        logger.warning(f"yfinance batch baseline failed: {e}")
    return result


def _fetch_bse_yfinance_batch(bse_codes: list) -> Dict[str, dict]:
    """
    Fetch BSE stock prices via direct Yahoo Finance HTTP API (parallel per-stock).
    Much faster and more reliable than yfinance batch download.
    Returns {BSE_CODE: {price, change_pct, open, high, low, volume, _exchange:'BSE'}}
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    result: Dict[str, dict] = {}
    if not bse_codes:
        return result

    _UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    YF_HEADERS = {"User-Agent": _UA, "Accept": "application/json"}

    def _yf_fetch_one(code: str):
        ticker = f"{code}.BO"
        for base in ("https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com"):
            try:
                url = f"{base}/v8/finance/chart/{ticker}?interval=1d&range=5d"
                r = requests.get(url, headers=YF_HEADERS, timeout=10)
                if r.status_code != 200:
                    continue
                data = r.json()
                res = data.get("chart", {}).get("result", [])
                if not res:
                    continue
                meta = res[0].get("meta", {})
                price = meta.get("regularMarketPrice") or meta.get("previousClose")
                prev  = meta.get("chartPreviousClose") or meta.get("previousClose") or price
                if not price or price <= 0 or price > 200_000:
                    continue
                quotes = res[0].get("indicators", {}).get("quote", [{}])[0]
                def _last(lst):
                    vals = [v for v in (lst or []) if v is not None]
                    return vals[-1] if vals else None
                o = _last(quotes.get("open"))  or price
                h = _last(quotes.get("high"))  or price
                l = _last(quotes.get("low"))   or price
                v = _last(quotes.get("volume")) or 0
                return code, {
                    "price":      round(float(price), 2),
                    "change_pct": round((price - prev) / prev * 100, 2) if prev else 0,
                    "open":       round(float(o), 2),
                    "high":       round(float(h), 2),
                    "low":        round(float(l), 2),
                    "volume":     int(v),
                    "mcap": None, "pe": None, "_exchange": "BSE",
                }
            except Exception:
                continue
        return code, None

    logger.info(f"BSE YF direct API: fetching {len(bse_codes)} stocks (50 workers)...")
    with ThreadPoolExecutor(max_workers=50) as ex:
        futs = {ex.submit(_yf_fetch_one, c): c for c in bse_codes}
        for fut in as_completed(futs):
            try:
                code, q = fut.result(timeout=15)
                if q:
                    result[code] = q
            except Exception:
                pass

    logger.info(f"BSE YF direct API: {len(result)}/{len(bse_codes)} stocks found")
    return result


def _fetch_single_quote(sym: str) -> tuple:
    """On-demand yfinance fetch for one symbol. Used only when bhav copy misses it."""
    import time as _time
    import io as _io
    import sys as _sys

    # Skip numeric BSE codes — Yahoo Finance has no NS/BO tickers for raw BSE codes
    if sym.isdigit():
        return sym, None

    # Check negative cache
    miss_ts = _yf_miss_cache.get(sym)
    if miss_ts and _time.time() - miss_ts < _YF_MISS_TTL:
        return sym, None

    try:
        import yfinance as yf
        # Suppress yfinance's "possibly delisted" stderr prints
        _devnull = _io.StringIO()
        for suffix in [".NS", ".BO"]:
            try:
                with _io.StringIO() as _stderr_sink:
                    _orig_stderr = _sys.stderr
                    _sys.stderr = _stderr_sink
                    try:
                        tk = yf.Ticker(f"{sym}{suffix}")
                        df = tk.history(period="2d", auto_adjust=True)
                    finally:
                        _sys.stderr = _orig_stderr

                if df.empty:
                    continue
                latest = df.iloc[-1]
                prev_c = float(df.iloc[-2]["Close"]) if len(df) > 1 else float(latest["Close"])
                price  = float(latest["Close"])
                if math.isnan(price) or price <= 0:
                    continue
                mcap, pe = None, None
                try:
                    fi = tk.fast_info
                    mc = getattr(fi, "market_cap", None)
                    if mc is not None and not math.isnan(float(mc)):
                        mcap = int(mc)
                except Exception:
                    pass
                try:
                    info   = tk.info
                    raw_pe = info.get("trailingPE") or info.get("forwardPE")
                    if raw_pe is not None and not math.isnan(float(raw_pe)):
                        pe = round(float(raw_pe), 1)
                except Exception:
                    pass
                return sym, {
                    "price":      round(price, 2),
                    "change_pct": round((price - prev_c) / prev_c * 100, 2),
                    "open":       round(float(latest.get("Open",  price)), 2),
                    "high":       round(float(latest.get("High",  price)), 2),
                    "low":        round(float(latest.get("Low",   price)), 2),
                    "volume":     int(latest.get("Volume", 0)),
                    "mcap":       mcap,
                    "pe":         pe,
                }
            except Exception:
                continue
    except Exception:
        pass
    # Record miss so we don't retry for 2 hours
    import time as _t
    _yf_miss_cache[sym] = _t.time()
    return sym, None


async def _fetch_yfinance_history(symbol: str, period: str = "1y") -> Optional[dict]:
    period_map = {"1d":"1d","1w":"5d","1m":"1mo","3m":"3mo","6m":"6mo","1y":"1y","5y":"5y"}
    yf_period = period_map.get(period, "1y")
    def _sync():
        try:
            import yfinance as yf, io as _io, sys as _sys
            df = None
            for suffix in [".NS", ".BO"]:
                _orig = _sys.stderr; _sys.stderr = _io.StringIO()
                try:
                    tk  = yf.Ticker(f"{symbol}{suffix}")
                    tmp = tk.history(period=yf_period, auto_adjust=True)
                finally:
                    _sys.stderr = _orig
                if not tmp.empty:
                    df = tmp; break
            if df is None or df.empty:
                return None
            return {
                "symbol":    symbol, "period": period,
                "dates":     [str(d.date()) for d in df.index],
                "prices":    [round(float(v), 2) for v in df["Close"]],
                "opens":     [round(float(v), 2) for v in df["Open"]],
                "highs":     [round(float(v), 2) for v in df["High"]],
                "lows":      [round(float(v), 2) for v in df["Low"]],
                "volumes":   [int(v) for v in df["Volume"]],
                "real_data": True,
            }
        except Exception as e:
            logger.warning(f"yfinance history failed for {symbol}: {e}")
            return None
    return await asyncio.get_event_loop().run_in_executor(None, _sync)


async def _fetch_yfinance_indices() -> dict:
    INDEX_MAP = {
        "NIFTY50":"^NSEI","SENSEX":"^BSESN","NIFTYBANK":"^NSEBANK",
        "NIFTYMID":"^NSEMDCP50","INDIAVIX":"^INDIAVIX",
    }
    def _sync():
        try:
            import yfinance as yf, pandas as pd, io as _io, sys as _sys
            _orig = _sys.stderr; _sys.stderr = _io.StringIO()
            try:
                raw = yf.download(list(INDEX_MAP.values()), period="2d", interval="1d",
                                  group_by="ticker", auto_adjust=True, progress=False)
            finally:
                _sys.stderr = _orig
            result = {}
            for name, tk in INDEX_MAP.items():
                try:
                    df = raw[tk].dropna(how="all") if isinstance(raw.columns, pd.MultiIndex) and tk in raw.columns.get_level_values(0) else raw.dropna(how="all")
                    if df is None or df.empty: continue
                    latest = float(df["Close"].iloc[-1])
                    prev   = float(df["Close"].iloc[-2]) if len(df) > 1 else latest
                    if math.isnan(latest): continue
                    change = round(latest - prev, 2)
                    pct    = round(change / prev * 100, 2) if prev else 0
                    result[name] = {"value": round(latest, 2), "change": change, "pct": pct}
                except Exception:
                    pass
            return result
        except Exception as e:
            logger.warning(f"Index fetch failed: {e}")
            return {}
    return await asyncio.get_event_loop().run_in_executor(None, _sync)


# ── Main DataFetcher ───────────────────────────────────────────
class DataFetcher:
    _raw_symbols: List[dict]   = []
    _stocks:      List[dict]   = []
    _real_quotes: Dict[str, dict] = {}
    symbol_count: int          = 0
    _bg_fetch_done: bool       = False
    # Persistent BSE code -> name cache. Unlike _raw_symbols (which gets wholesale
    # replaced whenever a fresh live symbol fetch completes), this dict is only ever
    # added to, so a code the live fetch fails to resolve still keeps its last-known name.
    _bse_name_cache: Dict[str, str] = {}

    # ── Disk cache ─────────────────────────────────────────────

    @classmethod
    def _load_price_cache(cls):
        if not os.path.exists(PRICE_CACHE_PATH):
            return
        try:
            with open(PRICE_CACHE_PATH, "r") as f:
                cached = json.load(f)
            cls._real_quotes.update(cached)
            print(f"✓ Price cache loaded: {len(cached)} stocks with prices (instant)")
        except Exception as e:
            logger.warning(f"Price cache load failed: {e}")

    @classmethod
    def _load_bse_name_map(cls):
        """Load persisted BSE code→name map and inject into _raw_symbols."""
        if not os.path.exists(BSE_NAME_MAP_PATH):
            return
        try:
            with open(BSE_NAME_MAP_PATH, "r") as f:
                name_map = json.load(f)  # {bse_code: name}
            existing_bse = {(r.get("bse-code") or "").strip() for r in cls._raw_symbols}
            added = 0
            for code, name in name_map.items():
                if not name or name.isdigit():
                    continue
                # Always populate the persistent fallback cache — this survives even
                # when _raw_symbols gets wholesale-replaced by a later live fetch.
                cls._bse_name_cache[code] = name
                if code not in existing_bse:
                    cls._raw_symbols.append({"name": name, "nse-code": "", "bse-code": code})
                    added += 1
            if added:
                logger.info(f"BSE name map: injected {added} names from disk cache")
        except Exception as e:
            logger.warning(f"BSE name map load failed: {e}")

    @classmethod
    def _save_bse_name_map(cls):
        """Persist BSE code→name so names survive restarts.

        Merges freshly-resolved names from _raw_symbols into the existing
        _bse_name_cache (union, never shrinks) so a code the live fetch fails to
        resolve this run doesn't lose the name it had from a previous run.
        """
        try:
            fresh = {
                r["bse-code"]: r["name"]
                for r in cls._raw_symbols
                if r.get("bse-code") and not r.get("nse-code")
                and r.get("name") and not r["name"].isdigit()
            }
            cls._bse_name_cache.update(fresh)
            name_map = cls._bse_name_cache
            if not name_map:
                return
            tmp = BSE_NAME_MAP_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(name_map, f)
            os.replace(tmp, BSE_NAME_MAP_PATH)
            logger.info(f"BSE name map saved: {len(name_map)} BSE-only stock names")
        except Exception as e:
            logger.warning(f"BSE name map save failed: {e}")

    @classmethod
    def _save_price_cache(cls):
        """Atomic write: write to .tmp then rename so a crash never corrupts the file."""
        tmp_path = PRICE_CACHE_PATH + ".tmp"
        try:
            with open(tmp_path, "w") as f:
                json.dump(cls._real_quotes, f)
            os.replace(tmp_path, PRICE_CACHE_PATH)
        except Exception as e:
            logger.warning(f"Price cache save failed: {e}")
            try:
                os.remove(tmp_path)
            except Exception:
                pass

    # ── Startup ────────────────────────────────────────────────

    @classmethod
    async def load_symbols(cls):
        # 1. Load disk cache FIRST — instant startup, no network wait
        cls._load_price_cache()
        cls._load_bse_name_map()   # inject persisted BSE names before first rebuild
        cls._rebuild_stocks()
        print(f"✓ {cls.symbol_count} stocks ready (cache) — {len(cls._real_quotes)} with prices")

        # 2. Fetch symbol names + fresh prices in background (non-blocking)
        asyncio.create_task(cls._fetch_symbol_names())
        asyncio.create_task(cls._background_fetch_all())

    @classmethod
    async def _fetch_symbol_names(cls):
        """
        Build the complete stock symbol index — deduplicating cross-listed stocks.

        Dedup strategy (in priority order):
          1. NSE_SYMBOL field from BSE → exact match against NSE code
          2. ISIN match between NSE and BSE lists
          3. analyst.indianapi.in cross-links (backup)

        Expected result: ~5,500–6,000 unique stocks (2,400 NSE + ~3,500 BSE-only).
        Without dedup, cross-listed stocks appear twice (e.g. 20MICRONS + 533022).
        """
        loop = asyncio.get_event_loop()

        # Final list: keyed by NSE code (for NSE-listed) or BSE code (for BSE-only)
        by_nse: Dict[str, dict] = {}   # nse_sym → entry
        by_isin: Dict[str, dict] = {}  # isin → entry (for ISIN dedup)
        bse_only: list = []            # accumulate BSE-only entries

        # ── 1. NSE official equity list ────────────────────────────
        try:
            nse_list = await loop.run_in_executor(None, fetch_nse_equity_list)
            for s in nse_list:
                sym  = s.get("nse-code", "").strip()
                isin = s.get("isin", "").strip()
                if not sym:
                    continue
                entry = {"name": s["name"], "nse-code": sym, "bse-code": "", "isin": isin}
                by_nse[sym] = entry
                if isin:
                    by_isin[isin] = entry
            logger.info(f"Symbol index step 1: {len(by_nse)} NSE stocks")
        except Exception as e:
            logger.warning(f"NSE equity list step failed: {e}")

        # ── 2. BSE official equity list — deduplicate via NSE_SYMBOL then ISIN ──
        try:
            bse_list = await loop.run_in_executor(None, fetch_bse_equity_list)
            cross_linked = 0
            for s in bse_list:
                bse_code = s.get("bse-code", "").strip()
                nse_sym  = s.get("nse-code", "").strip()   # BSE API's NSE_SYMBOL field
                isin     = s.get("isin", "").strip()
                if not bse_code:
                    continue

                if nse_sym and nse_sym in by_nse:
                    # Cross-listed: BSE_SYMBOL matches NSE entry → just fill BSE code
                    if not by_nse[nse_sym]["bse-code"]:
                        by_nse[nse_sym]["bse-code"] = bse_code
                    cross_linked += 1
                elif isin and isin in by_isin:
                    # Cross-listed by ISIN match
                    if not by_isin[isin]["bse-code"]:
                        by_isin[isin]["bse-code"] = bse_code
                    cross_linked += 1
                else:
                    # Truly BSE-only
                    bse_only.append({"name": s["name"], "nse-code": "", "bse-code": bse_code, "isin": isin})
            logger.info(
                f"Symbol index step 2: {cross_linked} cross-linked, "
                f"{len(bse_only)} BSE-only, total unique ≈ {len(by_nse) + len(bse_only)}"
            )
        except Exception as e:
            logger.warning(f"BSE equity list step failed: {e}")

        # ── 3. Fallback: analyst.indianapi.in (fills any remaining BSE codes on NSE entries) ──
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(ALL_STOCKS_URL)
                if resp.status_code == 200:
                    for s in resp.json():
                        nse = (s.get("nse-code") or "").strip()
                        bse = (s.get("bse-code") or "").strip()
                        if nse and nse in by_nse and bse and not by_nse[nse]["bse-code"]:
                            by_nse[nse]["bse-code"] = bse  # fill missing BSE code
                    logger.info("Symbol index step 3: fallback cross-links applied")
        except Exception as e:
            logger.warning(f"Fallback symbol list failed: {e}")

        # ── Merge and apply ────────────────────────────────────────
        # Deduplicate bse_only: remove any that already have bse-code in by_nse entries
        existing_bse = {v["bse-code"] for v in by_nse.values() if v.get("bse-code")}
        bse_only_deduped = [s for s in bse_only if s["bse-code"] not in existing_bse]

        raw = list(by_nse.values()) + bse_only_deduped
        logger.info(
            f"Symbol index final: {len(by_nse)} NSE + {len(bse_only_deduped)} BSE-only "
            f"= {len(raw)} unique stocks"
        )
        if len(raw) > len(cls._raw_symbols):
            cls._raw_symbols = raw
            cls._rebuild_stocks()
            logger.info(f"{cls.symbol_count} total stocks in UI")
            cls._save_bse_name_map()   # persist so names survive next restart

    # ── Background fetch — bhav copy + NSE live ────────────────

    @classmethod
    async def _background_fetch_all(cls):
        """
        Fetch all prices via official NSE/BSE sources. No rate limits.
        Order: yfinance baseline → NSE bhav copy → BSE bhav copy → NSE live

        IMPORTANT: yfinance batch runs FIRST for key stocks (including IDEA) so that
        the sanity filter has a correct price baseline even when bhav copy fails.
        NSE bhav copy then overwrites with official EOD data.
        NSE live runs LAST — any price deviating >40% from the baseline is rejected.
        This prevents bad NSE live data (e.g. IDEA returning ₹593 instead of ₹14)
        from ever entering the cache.
        """
        loop = asyncio.get_event_loop()

        # Step 0: yfinance batch baseline — fast cross-check for Nifty100 + known problem stocks
        # This runs even when bhav copy is unavailable (e.g. weekends, holidays, network issues)
        logger.info("Fetching yfinance baseline prices (Nifty100 + validation stocks)...")
        try:
            yf_syms = list(dict.fromkeys(NIFTY100_NSE + _YF_EXTRA_VALIDATE))  # dedup
            yf_base = await loop.run_in_executor(None, _fetch_yfinance_batch, yf_syms)
            if yf_base:
                # Only set baseline for stocks not already in cache, or where cache looks stale
                new_base = 0
                for sym, q in yf_base.items():
                    if sym not in cls._real_quotes:
                        cls._real_quotes[sym] = q
                        new_base += 1
                    else:
                        # If cached price differs >50% from yfinance → cache is bad, replace it
                        cached_price = cls._real_quotes[sym].get("price", 0)
                        yf_price = q.get("price", 0)
                        if cached_price > 0 and yf_price > 0:
                            ratio = abs(cached_price - yf_price) / yf_price
                            if ratio > 0.50:
                                logger.warning(
                                    f"BAD CACHE DETECTED {sym}: cached=₹{cached_price} "
                                    f"yfinance=₹{yf_price} ({ratio*100:.0f}% diff) — replacing with yfinance"
                                )
                                cls._real_quotes[sym] = q
                                new_base += 1
                cls._rebuild_stocks()
                cls._save_price_cache()
                logger.info(f"yfinance baseline: {len(yf_base)} fetched, {new_base} set/corrected")
        except Exception as e:
            logger.warning(f"yfinance baseline step failed: {e}")

        # Step 1: NSE Bhav Copy — FIRST so it establishes correct price baselines
        logger.info("Fetching NSE bhav copy (all NSE stocks)...")
        try:
            bhav = await loop.run_in_executor(None, fetch_nse_bhav_copy)
            if bhav:
                clean_bhav = _sanity_filter(bhav, cls._real_quotes)
                rejected = len(bhav) - len(clean_bhav)
                if rejected:
                    logger.warning(f"NSE bhav: {rejected} stocks rejected by sanity check")
                # Always overwrite cache with official bhav copy prices
                cls._real_quotes.update(clean_bhav)
                cls._rebuild_stocks()
                cls._save_price_cache()
                logger.info(f"NSE bhav copy: {len(clean_bhav)} stocks updated (baseline set)")
        except Exception as e:
            logger.warning(f"NSE bhav copy failed: {e}")

        # Step 2: BSE Bhav Copy + BSE yfinance fallback running IN PARALLEL
        # Bhav copy retries up to 5 days × 6 URLs (~30-60s to exhaust all attempts).
        # yfinance fallback for 2500+ stocks takes another ~60s.
        # Running both concurrently cuts total BSE wait from ~120s to ~60s.
        logger.info("Fetching BSE bhav copy + yfinance fallback in parallel...")
        try:
            # Compute which BSE-only codes need prices BEFORE launching (uses already-cached data)
            bse_missing_pre = [
                (r.get("bse-code") or "").strip()
                for r in cls._raw_symbols
                if (r.get("bse-code") or "").strip()
                and not (r.get("nse-code") or "").strip()
                and not cls._real_quotes.get((r.get("bse-code") or "").strip(), {}).get("price")
            ]
            if bse_missing_pre:
                logger.info(f"BSE: {len(bse_missing_pre)} stocks need prices — "
                            f"starting bhav copy + yfinance in parallel")

            # Launch both fetchers concurrently in thread pool
            bse_fut = loop.run_in_executor(None, fetch_bse_bhav_copy)
            yf_fut  = (loop.run_in_executor(None, _fetch_bse_yfinance_batch, bse_missing_pre)
                       if bse_missing_pre else None)

            bse    = await bse_fut
            bse_yf = (await yf_fut) if yf_fut else {}

            # ── Apply BSE bhav copy (authoritative EOD data — overwrites yfinance) ──
            if bse:
                clean_bse = _sanity_filter(bse, cls._real_quotes)
                rejected = len(bse) - len(clean_bse)
                if rejected:
                    logger.warning(f"BSE bhav: {rejected} stocks rejected by sanity check")
                new_bse = 0
                for sym, q in clean_bse.items():
                    cls._real_quotes[sym] = q   # always take fresh bhav copy price
                    new_bse += 1

                # Enrich _raw_symbols: add BSE-only stocks not already present
                # Build index of existing raw_symbols by bse-code for fast lookup
                existing_bse_idx = {
                    (r.get("bse-code") or "").strip(): i
                    for i, r in enumerate(cls._raw_symbols)
                }
                new_sym_entries = 0
                for code, q in bse.items():
                    sc_name = q.get("_name", "").strip()
                    if not sc_name or sc_name.isdigit():
                        continue
                    if code in existing_bse_idx:
                        # Update name if currently blank or just the numeric code
                        idx = existing_bse_idx[code]
                        cur_name = cls._raw_symbols[idx].get("name", "")
                        if not cur_name or cur_name.isdigit() or cur_name == code:
                            cls._raw_symbols[idx]["name"] = sc_name
                            new_sym_entries += 1
                    else:
                        cls._raw_symbols.append({"name": sc_name, "nse-code": "", "bse-code": code})
                        new_sym_entries += 1
                if new_sym_entries:
                    logger.info(f"BSE bhav: updated/added {new_sym_entries} BSE stock names")
                    cls._save_bse_name_map()   # persist new names from bhav copy

                cls._rebuild_stocks()
                cls._save_price_cache()
                logger.info(f"BSE bhav copy: {new_bse} stocks updated ✓")
            else:
                logger.warning("BSE bhav copy: 0 stocks — all URL formats failed "
                               "(check logs above for per-URL status codes)")

            # ── Apply yfinance results for stocks STILL missing after bhav copy ──
            if bse_yf:
                new_bse_yf = 0
                for code, q in bse_yf.items():
                    if not cls._real_quotes.get(code, {}).get("price"):
                        cls._real_quotes[code] = q
                        new_bse_yf += 1
                if new_bse_yf:
                    cls._rebuild_stocks()
                    cls._save_price_cache()
                    logger.info(f"BSE yfinance fallback: {new_bse_yf} additional stocks now have prices ✓")

            # ── Step 2c: BSE GROUP bulk API — ~15 requests cover ALL groups ──
            # Much safer than per-stock (no rate limit trigger). Runs after bhav copy + YF.
            still_missing_2c = [
                (r.get("bse-code") or "").strip()
                for r in cls._raw_symbols
                if (r.get("bse-code") or "").strip()
                and not (r.get("nse-code") or "").strip()
                and not cls._real_quotes.get((r.get("bse-code") or "").strip(), {}).get("price")
            ]
            if still_missing_2c:
                logger.info(f"BSE group API: {len(still_missing_2c)} still missing — "
                            f"fetching all BSE groups (~15 bulk requests)...")
                try:
                    bse_grp = await loop.run_in_executor(None, fetch_bse_prices_by_group)
                    if bse_grp:
                        new_grp = 0
                        for code, q in bse_grp.items():
                            if not cls._real_quotes.get(code, {}).get("price"):
                                cls._real_quotes[code] = q
                                new_grp += 1
                        if new_grp:
                            cls._rebuild_stocks()
                            cls._save_price_cache()
                            logger.info(f"BSE group API: {new_grp} additional stocks now have prices ✓")
                    else:
                        logger.warning("BSE group API: returned 0 results — may be blocked or unsupported")
                except Exception as e:
                    logger.warning(f"BSE group API step failed: {e}")

            # ── Step 2d: BSE per-stock API for STILL-missing BSE stocks ──
            # Last resort — individual stock JSON API (slow, rate-limit risk at high concurrency)
            still_missing = [
                (r.get("bse-code") or "").strip()
                for r in cls._raw_symbols
                if (r.get("bse-code") or "").strip()
                and not (r.get("nse-code") or "").strip()
                and not cls._real_quotes.get((r.get("bse-code") or "").strip(), {}).get("price")
            ]
            if still_missing:
                logger.info(f"BSE per-stock API: {len(still_missing)} stocks still missing — trying BSE JSON API...")
                try:
                    bse_api = await loop.run_in_executor(
                        None, fetch_bse_prices_per_stock, still_missing
                    )
                    if bse_api:
                        new_api = 0
                        for code, q in bse_api.items():
                            if not cls._real_quotes.get(code, {}).get("price"):
                                cls._real_quotes[code] = q
                                new_api += 1
                        if new_api:
                            cls._rebuild_stocks()
                            cls._save_price_cache()
                            logger.info(f"BSE per-stock API: {new_api} additional stocks now have prices ✓")
                    else:
                        logger.warning("BSE per-stock API: returned 0 results — API may be blocked or down")
                except Exception as e:
                    logger.warning(f"BSE per-stock API step failed: {e}")

            # Coverage summary
            bse_with_price = sum(
                1 for r in cls._raw_symbols
                if (r.get("bse-code") or "").strip()
                and not (r.get("nse-code") or "").strip()
                and cls._real_quotes.get((r.get("bse-code") or "").strip(), {}).get("price")
            )
            bse_total = sum(
                1 for r in cls._raw_symbols
                if (r.get("bse-code") or "").strip()
                and not (r.get("nse-code") or "").strip()
            )
            logger.info(f"BSE price coverage: {bse_with_price}/{bse_total} BSE-only stocks have prices")

        except Exception as e:
            logger.warning(f"BSE fetch step failed: {e}")

        # Step 3: NSE live market API — real-time overlay, validated against bhav baseline
        # Prices are only accepted if they pass the sanity filter (≤40% deviation from bhav).
        logger.info("Fetching NSE live market quotes...")
        try:
            live = await loop.run_in_executor(None, fetch_nse_live_quotes)
            if live:
                clean_live = _sanity_filter(live, cls._real_quotes)
                rejected = len(live) - len(clean_live)
                if rejected:
                    logger.warning(f"NSE live: {rejected} stocks rejected by sanity check (bad data from NSE API)")
                cls._real_quotes.update(clean_live)
                cls._rebuild_stocks()
                cls._save_price_cache()
                logger.info(f"NSE live: {len(clean_live)} stocks updated")
        except Exception as e:
            logger.warning(f"NSE live fetch failed: {e}")

        cls._bg_fetch_done = True
        print(f"✓ Prices complete: {len(cls._real_quotes)}/{cls.symbol_count} stocks")

        # Step 4: If market is open, keep refreshing live quotes every 5 minutes
        if is_market_open():
            asyncio.create_task(cls._realtime_refresh_loop())

    @classmethod
    async def _realtime_refresh_loop(cls):
        """Refresh NSE live market prices every 5 minutes during market hours."""
        loop = asyncio.get_event_loop()
        while is_market_open():
            await asyncio.sleep(90)    # 90 seconds — meets 1-2 min update requirement
            if not is_market_open():
                break
            try:
                live = await loop.run_in_executor(None, fetch_nse_live_quotes)
                if live:
                    clean = _sanity_filter(live, cls._real_quotes)
                    rejected = len(live) - len(clean)
                    if rejected:
                        logger.warning(f"Live refresh: {rejected} stocks rejected by sanity check")
                    cls._real_quotes.update(clean)
                    cls._rebuild_stocks()
                    logger.info(f"Live refresh: {len(clean)} prices updated")
            except Exception as e:
                logger.warning(f"Live refresh failed: {e}")
        logger.info("Market closed — live refresh stopped")

    # ── Stock list builder ─────────────────────────────────────

    @classmethod
    def _rebuild_stocks(cls):
        raw_by_nse: Dict[str, dict] = {}
        raw_by_bse: Dict[str, dict] = {}
        for raw in cls._raw_symbols:
            nse = (raw.get("nse-code") or "").strip()
            bse = (raw.get("bse-code") or "").strip()
            if nse and nse not in ("", "null", "None"):
                raw_by_nse[nse] = raw
            elif bse and bse not in ("", "null", "None"):
                raw_by_bse[bse] = raw

        stocks, seen = [], set()

        for sym, real in cls._real_quotes.items():
            if real.get("_exchange", "NSE") != "NSE": continue
            price = real.get("price", 0)
            if not price or math.isnan(price) or price <= 0: continue
            raw  = raw_by_nse.get(sym, {})
            name = raw.get("name", sym)
            stocks.append({"sym":sym,"name":name,"bse_code":raw.get("bse-code",""),"nse_code":sym,
                "exchange":"NSE","sector":_get_sector(name,sym),
                "price":real["price"],"change_pct":real["change_pct"],
                "open":real["open"],"high":real["high"],"low":real["low"],
                "volume":real["volume"],"pe":real.get("pe"),"mcap":real.get("mcap"),
                "ml_score":None,"has_nse":True,"real_data":True})
            seen.add(sym)

        for sym, raw in raw_by_nse.items():
            if sym in seen: continue
            name = raw.get("name", sym)
            stocks.append({"sym":sym,"name":name,"bse_code":raw.get("bse-code",""),"nse_code":sym,
                "exchange":"NSE","sector":_get_sector(name,sym),
                "price":0,"change_pct":0,"open":0,"high":0,"low":0,
                "volume":0,"pe":None,"mcap":None,
                "ml_score":None,"has_nse":True,"real_data":False})
            seen.add(sym)

        for sym, real in cls._real_quotes.items():
            if real.get("_exchange","NSE") != "BSE" or sym in seen: continue
            price = real.get("price", 0)
            if not price or math.isnan(price) or price <= 0: continue
            raw  = raw_by_bse.get(sym, {})
            name = raw.get("name") or real.get("_name", "").strip() or cls._bse_name_cache.get(sym) or sym
            stocks.append({"sym":sym,"name":name,"bse_code":sym,"nse_code":"",
                "exchange":"BSE","sector":_get_sector(name,sym),
                "price":real["price"],"change_pct":real["change_pct"],
                "open":real["open"],"high":real["high"],"low":real["low"],
                "volume":real["volume"],"pe":real.get("pe"),"mcap":real.get("mcap"),
                "ml_score":None,"has_nse":False,"real_data":True})
            seen.add(sym)

        for sym, raw in raw_by_bse.items():
            if sym in seen: continue
            name = raw.get("name") or cls._bse_name_cache.get(sym) or sym
            stocks.append({"sym":sym,"name":name,"bse_code":sym,"nse_code":"",
                "exchange":"BSE","sector":_get_sector(name,sym),
                "price":0,"change_pct":0,"open":0,"high":0,"low":0,
                "volume":0,"pe":None,"mcap":None,
                "ml_score":None,"has_nse":False,"real_data":False})
            seen.add(sym)

        cls._stocks      = stocks
        cls.symbol_count = len(stocks)

    # ── Public API ─────────────────────────────────────────────

    @classmethod
    async def get_all_stocks(cls, search="", exchange="", sector="", sort="name") -> List[dict]:
        data = list(cls._stocks)
        if search:
            q = search.lower()
            data = [s for s in data if q in s["name"].lower() or
                    q in (s["sym"] or "").lower() or
                    q in str(s.get("bse_code","")).lower()]
        if exchange == "bse":   data = [s for s in data if s["exchange"] == "BSE"]
        elif exchange == "nse": data = [s for s in data if s["exchange"] == "NSE"]
        if sector: data = [s for s in data if s["sector"] == sector]
        key_map = {
            "name":       lambda s: (not s["real_data"], s["name"]),
            "chg_desc":   lambda s: (not s["real_data"], -s["change_pct"]),
            "chg_asc":    lambda s: (not s["real_data"],  s["change_pct"]),
            "price_desc": lambda s: (not s["real_data"], -s["price"]),
            "vol_desc":   lambda s: (not s["real_data"], -s["volume"]),
            "mcap_desc":  lambda s: (not s["real_data"], -(s["mcap"] or 0)),
            "ml_desc":    lambda s: (not s["real_data"], -s["change_pct"]),
        }
        data.sort(key=key_map.get(sort, lambda s: (not s["real_data"], s["name"])))
        return data

    @classmethod
    async def get_quote(cls, symbol: str) -> Optional[dict]:
        sym_up = symbol.upper()
        for s in cls._stocks:
            if s["sym"] == sym_up or s.get("nse_code") == sym_up or str(s.get("bse_code","")) == sym_up:
                if s["real_data"]:
                    return s
                break
        # On-demand yfinance fetch (single stock only, no rate limit concern)
        loop = asyncio.get_event_loop()
        _, real = await loop.run_in_executor(None, _fetch_single_quote, sym_up)
        if not real:
            return None
        cls._real_quotes[sym_up] = real
        raw  = next((r for r in cls._raw_symbols if (r.get("nse-code") or "").strip() == sym_up), {})
        name = raw.get("name", sym_up)
        stock = {"sym":sym_up,"name":name,"bse_code":raw.get("bse-code",""),"nse_code":sym_up,
            "exchange":"NSE","sector":_get_sector(name,sym_up),
            "price":real["price"],"change_pct":real["change_pct"],
            "open":real["open"],"high":real["high"],"low":real["low"],
            "volume":real["volume"],"pe":real.get("pe"),"mcap":real.get("mcap"),
            "ml_score":None,"has_nse":True,"real_data":True}
        for i, s in enumerate(cls._stocks):
            if s["sym"] == sym_up:
                cls._stocks[i] = stock
                return stock
        cls._stocks.append(stock)
        cls.symbol_count = len(cls._stocks)
        return stock

    @classmethod
    async def get_history(cls, symbol: str, period: str = "1y") -> Optional[dict]:
        """Price history for a single symbol (yfinance-backed, cached 5 min)."""
        cache_key = f"history:{symbol}:{period}"
        cached = await Cache.get(cache_key)
        if cached:
            return cached
        data = await _fetch_yfinance_history(symbol, period)
        if data:
            await Cache.set(cache_key, data, ttl=300)
        return data

    @classmethod
    async def get_indices(cls) -> dict:
        """NIFTY/SENSEX/BANKNIFTY/etc snapshot (yfinance-backed, cached 1 min)."""
        cache_key = "market_indices"
        cached = await Cache.get(cache_key)
        if cached:
            return cached
        data = await _fetch_yfinance_indices()
        if data:
            await Cache.set(cache_key, data, ttl=60)
        return data

    @classmethod
    async def search(cls, q: str, limit: int = 10) -> List[dict]:
        """Quick symbol/name search over the in-memory stock list."""
        ql = q.lower()
        results = [
            s for s in cls._stocks
            if ql in s["name"].lower() or ql in (s["sym"] or "").lower()
            or ql in str(s.get("bse_code", "")).lower()
        ]
        results.sort(key=lambda s: (not s["real_data"], s["name"]))
        return results[:limit]
