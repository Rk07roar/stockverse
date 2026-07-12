"""
fill_bse_v2.py - Fast BSE price filler using parallel direct API calls.
Strategy:
  1. Yahoo Finance direct HTTP API per-stock (50 workers, no yfinance batch)
  2. BSE per-stock JSON API for remaining misses
  3. Saves to price_cache.json, then restart server

Run: python fill_bse_v2.py
"""
import json, os, time, requests, math
from concurrent.futures import ThreadPoolExecutor, as_completed

CACHE_PATH = os.path.join("backend", "data", "price_cache.json")
YF_WORKERS = 50   # parallel Yahoo Finance requests
BSE_WORKERS = 30  # parallel BSE API requests
SAVE_INTERVAL = 500  # save cache every N stocks processed

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

print("=" * 50)
print("  BSE Price Filler v2 - Fast parallel fetcher")
print("=" * 50)

# ─── Load existing cache ──────────────────────────────────────────────────────
print("\nLoading price_cache.json ...")
with open(CACHE_PATH) as f:
    cache = json.load(f)
print(f"  Loaded {len(cache)} entries")

# ─── Get all BSE codes ────────────────────────────────────────────────────────
print("\nFetching BSE equity list ...")
sess = requests.Session()
sess.headers.update({
    "User-Agent": _UA,
    "Referer": "https://www.bseindia.com/",
    "Accept": "application/json",
    "Origin": "https://www.bseindia.com",
})
try:
    sess.get("https://www.bseindia.com/", timeout=12)
    time.sleep(0.3)
except Exception:
    pass

BSE_CODES_FILE = "bse_codes_list.json"
url = "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w?segment=Equity&status=Active"
bse_codes = []
for _attempt in range(4):
    try:
        if _attempt > 0:
            print(f"  Retry {_attempt}/3 after 5s...")
            time.sleep(5)
        resp = sess.get(url, timeout=25)
        if not resp.text.strip() or resp.status_code != 200:
            print(f"  Attempt {_attempt+1}: HTTP {resp.status_code}, empty/blocked")
            continue
        raw = resp.json()
        rows = raw if isinstance(raw, list) else raw.get("Table", raw.get("data", []))
        bse_codes = [str(r.get("SCRIP_CD") or r.get("scrip_cd") or "").strip() for r in rows
                     if str(r.get("SCRIP_CD") or r.get("scrip_cd") or "").strip()]
        if bse_codes:
            # Save for future fallback
            with open(BSE_CODES_FILE, "w") as _f:
                json.dump(bse_codes, _f)
            print(f"  Saved {len(bse_codes)} BSE codes to {BSE_CODES_FILE}")
            break
    except Exception as e:
        print(f"  Attempt {_attempt+1} failed: {e}")

if not bse_codes:
    # Fallback 1: load from saved file
    if os.path.exists(BSE_CODES_FILE):
        try:
            with open(BSE_CODES_FILE) as _f:
                bse_codes = json.load(_f)
            print(f"  Loaded {len(bse_codes)} BSE codes from saved file {BSE_CODES_FILE}")
        except Exception:
            pass
    # Fallback 2: BSE codes already in cache
    if not bse_codes:
        print("  BSE equity list API unavailable, using codes from cache...")
        bse_codes = [k for k in cache.keys() if k.isdigit()]
        print(f"  Using {len(bse_codes)} codes from cache as fallback")

print(f"  Got {len(bse_codes)} BSE codes")

missing = [c for c in bse_codes if not cache.get(c, {}).get("price")]
print(f"  {len(missing)} need prices")

# ─── Step 0: BSE Group Bulk API (~15 requests = ALL groups, no rate limit!) ──
print(f"\nStep 0: BSE GROUP bulk API (~15 requests covers all BSE groups)...")

BSE_GROUPS = ["A", "B", "F", "G", "M", "S", "T", "X", "XT", "XD", "Z",
              "IP", "SM", "SY", "IV", "BE", "BZ", "BO", "IF", "N", "R", "W"]

BSE_HDRS0 = {
    "User-Agent": _UA,
    "Referer": "https://www.bseindia.com/",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.bseindia.com",
    "X-Requested-With": "XMLHttpRequest",
}

sess_grp = requests.Session()
sess_grp.headers.update(BSE_HDRS0)
try:
    r0 = sess_grp.get("https://www.bseindia.com/", timeout=12)
    print(f"  BSE session: HTTP {r0.status_code}")
    time.sleep(0.5)
except Exception as e:
    print(f"  BSE session: {e}")

grp_found = 0
grp_rate_limited = False

for group in BSE_GROUPS:
    try:
        url_grp = (f"https://api.bseindia.com/BseIndiaAPI/api/"
                   f"GetLatestStockPricedataByGroupNew/w?Group={group}")
        rg = sess_grp.get(url_grp, timeout=20, headers=BSE_HDRS0)
        if rg.status_code == 403:
            print(f"  Group {group}: HTTP 403 — RATE LIMITED, stopping group fetch")
            grp_rate_limited = True
            break
        if rg.status_code != 200:
            print(f"  Group {group}: HTTP {rg.status_code} — skip")
            continue
        try:
            data = rg.json()
        except Exception:
            print(f"  Group {group}: bad JSON")
            continue

        rows = data if isinstance(data, list) else data.get("Table", data.get("data", []))
        if not rows:
            print(f"  Group {group}: 0 rows (unsupported group)")
            continue

        grp_new = 0
        for row in rows:
            code = str(
                row.get("scripcode") or row.get("SCRIP_CD") or row.get("ScripCode")
                or row.get("scripCode") or ""
            ).strip()
            if not code or cache.get(code, {}).get("price"):
                continue  # already have it

            # Price extraction
            curr_rate = row.get("CurrRate")
            chg_direct = None
            if isinstance(curr_rate, dict):
                close_raw = curr_rate.get("LTP") or curr_rate.get("ltp") or 0
                try:
                    chg_direct = float(str(curr_rate.get("PcChg") or "0").replace(",", "").replace("+", ""))
                except Exception:
                    pass
            else:
                close_raw = (row.get("CurrPric") or row.get("LTP") or row.get("ltp")
                             or curr_rate or row.get("Close") or row.get("CLOSE") or 0)
            try:
                close = float(str(close_raw).replace(",", ""))
            except Exception:
                continue
            if close <= 0 or close > 200_000:
                continue

            prev_raw = row.get("PrevClos") or row.get("prevclose") or close_raw
            try:
                prev = float(str(prev_raw).replace(",", "") or close)
            except Exception:
                prev = close

            if chg_direct is None:
                try:
                    cs = str(row.get("ChangePer") or row.get("PcChg") or "")
                    chg_direct = float(cs.replace(",", "").replace("+", "")) if cs else None
                except Exception:
                    pass
            change_pct = (round(chg_direct, 2) if chg_direct is not None
                          else round((close - prev) / prev * 100, 2) if prev else 0)

            vol_raw = (row.get("TotalNoofSharesTraded") or row.get("Volume")
                       or row.get("volume") or row.get("TtlTradgVol") or 0)
            try:
                vol = int(float(str(vol_raw).replace(",", "")))
            except Exception:
                vol = 0

            def _gf(keys, d=close):
                for k in keys:
                    v = row.get(k)
                    if v not in (None, "", "0", 0):
                        try:
                            return round(float(str(v).replace(",", "")), 2)
                        except Exception:
                            pass
                return round(float(d), 2)

            cache[code] = {
                "price": round(close, 2), "change_pct": change_pct,
                "open": _gf(["Open", "open", "OpnPric"]),
                "high": _gf(["High", "high", "HghPric"]),
                "low":  _gf(["Low",  "low",  "LwPric"]),
                "volume": vol, "mcap": None, "pe": None, "_exchange": "BSE",
            }
            grp_new += 1
            grp_found += 1

        if grp_new > 0:
            print(f"  Group {group}: {grp_new} stocks ✓  (total so far: {grp_found})")
        time.sleep(0.3)
    except Exception as e:
        print(f"  Group {group}: {e}")

print(f"\n  BSE Group API: {grp_found} new stocks found")
if grp_found > 0:
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f)
    os.replace(tmp, CACHE_PATH)
    print(f"  Saved interim cache ({len(cache)} total entries)")

# Recalculate missing after group step
missing = [c for c in bse_codes if not cache.get(c, {}).get("price")]
print(f"  Still missing after group fetch: {len(missing)}")

# ─── Step 1: Yahoo Finance direct HTTP API (parallel) ────────────────────────
print(f"\nStep 1: Yahoo Finance direct API ({len(missing)} stocks, {YF_WORKERS} workers)...")
print("  (This is ~20-40x faster than yfinance batch)")

YF_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/json",
}

def _yf_fetch(code):
    """Fetch single stock from Yahoo Finance direct API."""
    ticker = f"{code}.BO"
    urls = [
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d",
        f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d",
    ]
    for url in urls:
        try:
            r = requests.get(url, headers=YF_HEADERS, timeout=10)
            if r.status_code != 200:
                continue
            data = r.json()
            result = data.get("chart", {}).get("result", [])
            if not result:
                continue
            meta = result[0].get("meta", {})
            price = meta.get("regularMarketPrice") or meta.get("previousClose")
            prev  = meta.get("chartPreviousClose") or meta.get("previousClose") or price
            if not price or price <= 0 or price > 200_000:
                continue
            # Try to get OHLCV from indicators
            indicators = result[0].get("indicators", {})
            quotes = indicators.get("quote", [{}])[0]
            def _last(lst):
                if lst:
                    vals = [v for v in lst if v is not None]
                    return vals[-1] if vals else None
                return None
            o = _last(quotes.get("open"))  or price
            h = _last(quotes.get("high"))  or price
            l = _last(quotes.get("low"))   or price
            v = _last(quotes.get("volume")) or 0
            chg = round((price - prev) / prev * 100, 2) if prev else 0
            return code, {
                "price":      round(float(price), 2),
                "change_pct": chg,
                "open":       round(float(o), 2),
                "high":       round(float(h), 2),
                "low":        round(float(l), 2),
                "volume":     int(v),
                "mcap": None, "pe": None, "_exchange": "BSE",
            }
        except Exception:
            continue
    return code, None

yf_found = 0
processed = 0
start = time.time()

with ThreadPoolExecutor(max_workers=YF_WORKERS) as ex:
    futs = {ex.submit(_yf_fetch, c): c for c in missing}
    for fut in as_completed(futs):
        processed += 1
        try:
            code, q = fut.result(timeout=15)
            if q and not cache.get(code, {}).get("price"):
                cache[code] = q
                yf_found += 1
        except Exception:
            pass
        if processed % 250 == 0:
            elapsed = time.time() - start
            rate = processed / elapsed
            remaining = (len(missing) - processed) / rate if rate else 0
            print(f"  {processed}/{len(missing)} checked | {yf_found} found | "
                  f"{rate:.0f}/s | ETA {remaining:.0f}s")
            # Incremental save
            tmp = CACHE_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(cache, f)
            os.replace(tmp, CACHE_PATH)

elapsed = time.time() - start
print(f"\n  Yahoo Finance: {yf_found}/{len(missing)} found in {elapsed:.0f}s")

# ─── Step 2: BSE per-stock API for remaining stocks ───────────────────────────
still_missing = [c for c in bse_codes if not cache.get(c, {}).get("price")]
print(f"\nStep 2: BSE per-stock API for {len(still_missing)} remaining...")

BSE_API_TEMPLATES = [
    "https://api.bseindia.com/BseIndiaAPI/api/getScripHeaderData/w?scripcode={code}",
    "https://api.bseindia.com/BseIndiaAPI/api/ComHeader/w?quotetype=EQ&scripcode={code}",
    "https://api.bseindia.com/BseIndiaAPI/api/marketWatch/w?scripcode={code}&seriesid=EQ",
]

BSE_HDRS = {
    "User-Agent": _UA,
    "Referer":    "https://www.bseindia.com/",
    "Accept":     "application/json, text/plain, */*",
    "Origin":     "https://www.bseindia.com",
    "X-Requested-With": "XMLHttpRequest",
}

# Quick API test first
print("  Testing BSE API with Reliance (500325)...")
try:
    r = sess.get("https://api.bseindia.com/BseIndiaAPI/api/getScripHeaderData/w?scripcode=500325",
                 timeout=8, headers=BSE_HDRS)
    print(f"  HTTP {r.status_code} | {r.text[:120]!r}")
except Exception as e:
    print(f"  Test failed: {e}")

def _bse_fetch(code):
    for url_tpl in BSE_API_TEMPLATES:
        try:
            r = sess.get(url_tpl.format(code=code), timeout=8, headers=BSE_HDRS)
            if r.status_code != 200:
                continue
            if r.content[:20].lstrip().startswith(b'<'):
                continue
            try:
                data = r.json()
            except Exception:
                continue
            if isinstance(data, list) and data:
                data = data[0]
            if not isinstance(data, dict) or not data:
                continue
            # Handle nested CurrRate dict (main BSE API format)
            # e.g. {"CurrRate": {"LTP": "1326.55", "Chg": "+17.20", "PcChg": "+1.31", ...}}
            curr_rate = data.get("CurrRate")
            if isinstance(curr_rate, dict):
                close_raw = curr_rate.get("LTP") or curr_rate.get("ltp") or 0
                # PcChg already gives us % change directly
                try:
                    chg_direct = float(str(curr_rate.get("PcChg") or "0").replace(",", "").replace("+", ""))
                except Exception:
                    chg_direct = None
            else:
                close_raw = (data.get("CurrPric") or curr_rate or data.get("LTP")
                             or data.get("ltp") or data.get("close") or data.get("Close")
                             or data.get("LastTradedPrice") or data.get("PrevClos") or 0)
                chg_direct = None
            if not close_raw or str(close_raw) in ("0", "0.00", ""):
                continue
            try:
                close = float(str(close_raw).replace(",", ""))
            except (ValueError, TypeError):
                continue
            if close <= 0 or close > 200_000:
                continue
            prev_raw = (data.get("PrevClos") or data.get("prevclose") or data.get("PreviousClose")
                        or data.get("prev_close") or close_raw)
            try:
                prev = float(str(prev_raw).replace(",", "") or close)
            except (ValueError, TypeError):
                prev = close
            vol_raw = (data.get("TotalNoofSharesTraded") or data.get("Volume")
                       or data.get("volume") or data.get("TtlTradgVol") or 0)
            try:
                vol = int(float(str(vol_raw).replace(",", "")))
            except (ValueError, TypeError):
                vol = 0
            # Use direct % change from API if available, else calculate
            if chg_direct is not None:
                change_pct = round(chg_direct, 2)
            else:
                change_pct = round((close - prev) / prev * 100, 2) if prev else 0
            def _safe_float(key, default):
                try:
                    return round(float(str(data.get(key) or default).replace(",", "")), 2)
                except Exception:
                    return round(float(default), 2)
            return code, {
                "price":      round(close, 2),
                "change_pct": change_pct,
                "open":  _safe_float("Open",  close),
                "high":  _safe_float("High",  close),
                "low":   _safe_float("Low",   close),
                "volume": vol,
                "mcap": None, "pe": None, "_exchange": "BSE",
            }
        except Exception:
            continue
    return code, None

bse_found = 0
bse_processed = 0
bse_start = time.time()

if still_missing:
    with ThreadPoolExecutor(max_workers=BSE_WORKERS) as ex:
        futs = {ex.submit(_bse_fetch, c): c for c in still_missing}
        for fut in as_completed(futs):
            bse_processed += 1
            try:
                code, q = fut.result(timeout=15)
                if q and not cache.get(code, {}).get("price"):
                    cache[code] = q
                    bse_found += 1
            except Exception:
                pass
            if bse_processed % 200 == 0:
                elapsed2 = time.time() - bse_start
                rate2 = bse_processed / elapsed2 if elapsed2 > 0 else 1
                rem2 = (len(still_missing) - bse_processed) / rate2
                print(f"  {bse_processed}/{len(still_missing)} | {bse_found} found | ETA {rem2:.0f}s")
                tmp = CACHE_PATH + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(cache, f)
                os.replace(tmp, CACHE_PATH)
    print(f"  BSE API: {bse_found}/{len(still_missing)} found")
else:
    print("  Nothing remaining - skip")

# ─── Final save ────────────────────────────────────────────────────────────────
bse_total = sum(1 for v in cache.values() if v.get("_exchange") == "BSE")
nse_total = sum(1 for v in cache.values() if v.get("_exchange", "NSE") == "NSE")
total_with_price = sum(1 for v in cache.values() if v.get("price"))

print(f"\n{'='*50}")
print(f"Final: {len(cache)} total entries")
print(f"  NSE: {nse_total} | BSE: {bse_total}")
print(f"  Entries with price: {total_with_price}")
print(f"  New from YF: {yf_found} | New from BSE API: {bse_found}")
print(f"{'='*50}")

tmp = CACHE_PATH + ".tmp"
with open(tmp, "w") as f:
    json.dump(cache, f)
os.replace(tmp, CACHE_PATH)
print(f"\nSaved to {CACHE_PATH}")
print("\nNow double-click setup_and_run.bat to restart the server!")
