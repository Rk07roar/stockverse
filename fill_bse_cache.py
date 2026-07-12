"""
fill_bse_cache.py — Run once from stockvest_full\ folder to populate BSE prices.

1. Reads all BSE-only codes from the BSE equity list API
2. Fetches prices via yfinance .BO in batches (primary)
3. Falls back to BSE per-stock JSON API for missing ones
4. Merges results into price_cache.json
5. Reports final coverage

Run: python fill_bse_cache.py
"""
import json, os, math, time, requests, sys, io
from concurrent.futures import ThreadPoolExecutor, as_completed

CACHE_PATH = os.path.join("backend", "data", "price_cache.json")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# ─── Load existing cache ──────────────────────────────────────────────────────
print("Loading price_cache.json ...")
with open(CACHE_PATH) as f:
    cache = json.load(f)
print(f"  Loaded {len(cache)} entries")

# ─── Get all BSE codes from equity list API ───────────────────────────────────
print("\nFetching BSE equity list ...")
bse_session = requests.Session()
bse_session.headers.update({"User-Agent": _UA, "Referer": "https://www.bseindia.com/",
                              "Accept": "application/json", "Origin": "https://www.bseindia.com"})
bse_session.get("https://www.bseindia.com/", timeout=12)
time.sleep(0.3)

url = "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w?segment=Equity&status=Active"
resp = bse_session.get(url, timeout=20)
rows = resp.json() if isinstance(resp.json(), list) else resp.json().get("Table", resp.json().get("data", []))
bse_codes = [str(r.get("SCRIP_CD") or r.get("scrip_cd") or "").strip() for r in rows
             if str(r.get("SCRIP_CD") or r.get("scrip_cd") or "").strip()]
print(f"  Got {len(bse_codes)} BSE codes")

# Find which ones need prices
missing = [c for c in bse_codes if not cache.get(c, {}).get("price")]
print(f"  {len(missing)} codes still need prices")

# ─── Step 1: yfinance .BO batch ───────────────────────────────────────────────
print(f"\nStep 1: yfinance .BO batch ({len(missing)} stocks, may take 2-3 min)...")
yf_results = {}
try:
    import yfinance as yf
    import pandas as pd
    CHUNK = 200
    chunks = [missing[i:i+CHUNK] for i in range(0, len(missing), CHUNK)]
    for idx, chunk in enumerate(chunks):
        tickers = [f"{c}.BO" for c in chunk]
        _orig = sys.stderr; sys.stderr = io.StringIO()
        try:
            raw = yf.download(tickers, period="5d", interval="1d",
                              auto_adjust=True, progress=False,
                              group_by="ticker", threads=True, timeout=30)
        finally:
            sys.stderr = _orig
        if raw is None or raw.empty:
            print(f"  Chunk {idx+1}/{len(chunks)}: empty data")
            continue
        got = 0
        for code in chunk:
            tk = f"{code}.BO"
            try:
                df = (raw[tk].dropna(how="all") if isinstance(raw.columns, pd.MultiIndex)
                              and tk in raw.columns.get_level_values(0)
                      else raw.dropna(how="all"))
                if df.empty: continue
                price = float(df["Close"].iloc[-1])
                prev  = float(df["Close"].iloc[-2]) if len(df) > 1 else price
                if price <= 0 or math.isnan(price) or price > 200_000: continue
                yf_results[code] = {
                    "price":      round(price, 2),
                    "change_pct": round((price - prev) / prev * 100, 2) if prev else 0,
                    "open":  round(float(df["Open"].iloc[-1] or price), 2),
                    "high":  round(float(df["High"].iloc[-1] or price), 2),
                    "low":   round(float(df["Low"].iloc[-1]  or price), 2),
                    "volume": int(df["Volume"].iloc[-1] or 0),
                    "mcap": None, "pe": None, "_exchange": "BSE",
                }
                got += 1
            except Exception:
                pass
        print(f"  Chunk {idx+1}/{len(chunks)}: {got} prices found (total so far: {len(yf_results)})")
except Exception as e:
    print(f"  yfinance error: {e}")

print(f"  yfinance total: {len(yf_results)}/{len(missing)} BSE stocks")

# Apply yfinance results to cache
new_yf = 0
for code, q in yf_results.items():
    if not cache.get(code, {}).get("price"):
        cache[code] = q
        new_yf += 1
print(f"  Added {new_yf} new entries from yfinance")

# ─── Step 2: BSE per-stock JSON API for still-missing stocks ─────────────────
still_missing = [c for c in bse_codes if not cache.get(c, {}).get("price")]
print(f"\nStep 2: BSE per-stock API for {len(still_missing)} remaining stocks...")

API_TEMPLATES = [
    "https://api.bseindia.com/BseIndiaAPI/api/getScripHeaderData/w?scripcode={code}",
    "https://api.bseindia.com/BseIndiaAPI/api/ComHeader/w?quotetype=EQ&scripcode={code}",
    "https://api.bseindia.com/BseIndiaAPI/api/marketWatch/w?scripcode={code}&seriesid=EQ",
]

api_headers = {
    "User-Agent": _UA,
    "Referer":    "https://www.bseindia.com/",
    "Accept":     "application/json, text/plain, */*",
    "Origin":     "https://www.bseindia.com",
    "X-Requested-With": "XMLHttpRequest",
}

# Print sample of what API returns for debugging
print("  Testing BSE API for code 500325 (Reliance)...")
try:
    r = bse_session.get("https://api.bseindia.com/BseIndiaAPI/api/getScripHeaderData/w?scripcode=500325",
                        timeout=8, headers=api_headers)
    print(f"  HTTP {r.status_code} | CT={r.headers.get('Content-Type','?')[:50]}")
    print(f"  Response: {r.text[:200]!r}")
    if r.status_code == 200 and 'json' in r.headers.get('Content-Type','').lower():
        data = r.json()
        print(f"  Keys: {list(data.keys())[:15] if isinstance(data, dict) else type(data)}")
except Exception as e:
    print(f"  Test failed: {e}")

def _fetch_bse_api(code):
    for url_tpl in API_TEMPLATES:
        try:
            r = bse_session.get(url_tpl.format(code=code), timeout=8, headers=api_headers)
            if r.status_code != 200:
                continue
            ct = r.headers.get("Content-Type", "")
            text = r.text.strip()
            if b"<!DOCTYPE" in r.content[:50] or b"<html" in r.content[:50]:
                continue
            try:
                data = r.json()
            except Exception:
                continue
            if isinstance(data, list) and data:
                data = data[0]
            if not isinstance(data, dict) or not data:
                continue
            # Try to extract price — BSE API uses various field names
            close_raw = (data.get("CurrPric") or data.get("CurrRate") or data.get("LTP")
                         or data.get("ltp") or data.get("close") or data.get("Close")
                         or data.get("LastTradedPrice") or data.get("PrevClos") or 0)
            if not close_raw or close_raw == "0":
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
            return code, {
                "price":      round(close, 2),
                "change_pct": round((close - prev) / prev * 100, 2) if prev else 0,
                "open":       round(float(str(data.get("Open") or data.get("open") or close).replace(",", "")), 2),
                "high":       round(float(str(data.get("High") or data.get("high") or close).replace(",", "")), 2),
                "low":        round(float(str(data.get("Low")  or data.get("low")  or close).replace(",", "")), 2),
                "volume":     vol,
                "mcap": None, "pe": None, "_exchange": "BSE",
            }
        except Exception:
            continue
    return code, None

api_results = {}
if still_missing:
    with ThreadPoolExecutor(max_workers=25) as ex:
        futs = {ex.submit(_fetch_bse_api, c): c for c in still_missing}
        done = 0
        for fut in as_completed(futs):
            try:
                code, q = fut.result(timeout=15)
                if q:
                    api_results[code] = q
            except Exception:
                pass
            done += 1
            if done % 200 == 0:
                print(f"  {done}/{len(still_missing)} done, {len(api_results)} prices found")
    print(f"  BSE API total: {len(api_results)}/{len(still_missing)} stocks")
    # Apply to cache
    new_api = 0
    for code, q in api_results.items():
        if not cache.get(code, {}).get("price"):
            cache[code] = q
            new_api += 1
    print(f"  Added {new_api} new entries from BSE API")
else:
    print("  No stocks remaining — skip")

# ─── Save cache ───────────────────────────────────────────────────────────────
bse_total = sum(1 for v in cache.values() if v.get("_exchange") == "BSE")
nse_total = sum(1 for v in cache.values() if v.get("_exchange", "NSE") == "NSE")
print(f"\nFinal cache: {len(cache)} total ({nse_total} NSE + {bse_total} BSE)")

tmp = CACHE_PATH + ".tmp"
with open(tmp, "w") as f:
    json.dump(cache, f)
os.replace(tmp, CACHE_PATH)
print(f"Saved to {CACHE_PATH}")
print("\nDone! Restart the server (setup_and_run.bat) to load the updated cache.")
