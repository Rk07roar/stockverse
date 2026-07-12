"""
BSE per-stock API test — run from backend/ folder.
Tests which JSON endpoints return price data for individual BSE codes.
"""
import requests, json, time, concurrent.futures

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Representative BSE codes to test
TEST_CODES = ["500325", "532540", "500180", "500112", "532174",
              "500820", "500010", "500696", "507685", "540769"]

output_lines = []
def log(msg):
    print(msg)
    output_lines.append(str(msg))

log("=== BSE Per-Stock API Test ===\n")

session = requests.Session()
session.headers.update({
    "User-Agent": _BROWSER_UA,
    "Referer":    "https://www.bseindia.com/",
    "Accept":     "application/json, text/plain, */*",
    "Origin":     "https://www.bseindia.com",
    "X-Requested-With": "XMLHttpRequest",
})

# Visit BSE homepage to get cookies
log("Setting up BSE session...")
try:
    r = session.get("https://www.bseindia.com/", timeout=12)
    log(f"  Homepage: HTTP {r.status_code}, cookies: {list(session.cookies.keys())}")
    time.sleep(0.5)
except Exception as e:
    log(f"  Homepage failed: {e}")

# Test different per-stock API endpoints
api_endpoints = [
    # Format: (name, url_template)
    ("ScripHeaderData",   "https://api.bseindia.com/BseIndiaAPI/api/getScripHeaderData/w?scripcode={code}"),
    ("QuotesScrData",     "https://api.bseindia.com/BseIndiaAPI/api/getQuotesScrData/w?scripcode={code}&group=A"),
    ("ComHeader",         "https://api.bseindia.com/BseIndiaAPI/api/ComHeader/w?quotetype=EQ&scripcode={code}"),
    ("RealTimeData",      "https://api.bseindia.com/BseIndiaAPI/api/RealTimeData/w?scripcode={code}"),
    ("marketWatch",       "https://api.bseindia.com/BseIndiaAPI/api/marketWatch/w?scripcode={code}&seriesid=EQ"),
    ("stockReachGraph",   "https://api.bseindia.com/BseIndiaAPI/api/stockReachGraph/w?scripcode={code}&seriesid=EQ"),
]

log(f"\nTesting per-stock APIs with BSE code 500325 (Reliance):\n")
code = "500325"
for name, url_tpl in api_endpoints:
    url = url_tpl.format(code=code)
    try:
        r = session.get(url, timeout=10)
        ct = r.headers.get("Content-Type", "?")[:50]
        is_json = "json" in ct.lower() or (r.text.strip().startswith("{") or r.text.strip().startswith("["))
        log(f"  [{name}] HTTP {r.status_code} | CT={ct}")
        if is_json and r.status_code == 200:
            try:
                data = r.json()
                log(f"    JSON KEYS: {list(data.keys())[:10] if isinstance(data, dict) else type(data).__name__}")
                # Look for price fields
                price_keys = [k for k in (data.keys() if isinstance(data, dict) else [])
                              if any(p in k.lower() for p in ['close', 'price', 'ltp', 'last', 'pric'])]
                if price_keys:
                    log(f"    PRICE FIELDS: {[(k, data[k]) for k in price_keys[:5]]}")
                    log(f"    *** WORKS! Found price data ***")
                else:
                    log(f"    First 200 chars: {str(data)[:200]}")
            except Exception as je:
                log(f"    JSON parse error: {je}")
                log(f"    Raw: {r.text[:150]!r}")
        else:
            log(f"    First 150 bytes: {r.content[:150]!r}")
    except Exception as e:
        log(f"  [{name}] ERROR: {e}")
    log("")

# Test Yahoo Finance direct API (alternative to yfinance batch)
log("\nTesting Yahoo Finance direct API for BSE stocks:\n")
yf_urls = [
    f"https://query1.finance.yahoo.com/v8/finance/chart/500325.BO?interval=1d&range=5d",
    f"https://query2.finance.yahoo.com/v8/finance/chart/500325.BO?interval=1d&range=5d",
    f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/500325.BO?modules=price",
]
for url in yf_urls:
    try:
        r = session.get(url, timeout=10, headers={"Accept": "application/json"})
        ct = r.headers.get("Content-Type", "?")[:50]
        log(f"  HTTP {r.status_code} | CT={ct}")
        log(f"  URL: {url[-60:]}")
        if r.status_code == 200:
            try:
                data = r.json()
                # Check for price
                chart = data.get("chart", {}) or data.get("quoteSummary", {})
                log(f"  Data keys: {list(data.keys())[:5]}")
                result = chart.get("result", [])
                if result:
                    meta = result[0].get("meta", {})
                    log(f"  Price found: regularMarketPrice={meta.get('regularMarketPrice')}, previousClose={meta.get('chartPreviousClose')}")
                    log(f"  *** WORKS! ***")
            except Exception as je:
                log(f"  JSON error: {je}, raw: {r.text[:100]!r}")
    except Exception as e:
        log(f"  ERROR: {e}")
    log("")

# Test Stooq (free data provider with BSE data)
log("\nTesting Stooq (alternative data provider):\n")
stooq_urls = [
    "https://stooq.com/q/l/?s=500325.BO&f=sd2t2ohlcv&h&e=csv",
    "https://stooq.com/q/l/?s=reliance.BO&f=sd2t2ohlcv&h&e=csv",
]
for url in stooq_urls:
    try:
        r = session.get(url, timeout=10, headers={"Accept": "*/*"})
        log(f"  HTTP {r.status_code} | CT={r.headers.get('Content-Type','?')[:40]}")
        log(f"  URL: {url[-60:]}")
        log(f"  Content: {r.text[:200]!r}")
    except Exception as e:
        log(f"  ERROR: {e}")
    log("")

# Test how many BSE codes yfinance can actually fetch
log("\nTesting yfinance batch for 10 BSE codes:\n")
try:
    import yfinance as yf, io as _io, sys as _sys
    tickers = [f"{c}.BO" for c in TEST_CODES]
    log(f"  Fetching: {tickers}")
    orig = _sys.stderr; _sys.stderr = _io.StringIO()
    try:
        raw = yf.download(tickers, period="5d", interval="1d", auto_adjust=True, progress=False, group_by="ticker", threads=True)
    finally:
        _sys.stderr = orig
    if raw is not None and not raw.empty:
        found = []
        import pandas as pd
        for code in TEST_CODES:
            ticker = f"{code}.BO"
            try:
                if isinstance(raw.columns, pd.MultiIndex):
                    if ticker in raw.columns.get_level_values(0):
                        df = raw[ticker].dropna(how="all")
                        if not df.empty:
                            found.append(f"{code}={float(df['Close'].iloc[-1]):.2f}")
                else:
                    df = raw.dropna(how="all")
                    if not df.empty:
                        found.append(f"{code}={float(df['Close'].iloc[-1]):.2f}")
            except Exception:
                pass
        log(f"  Found prices: {found}")
        log(f"  {len(found)}/{len(TEST_CODES)} stocks have yfinance .BO data")
    else:
        log("  yfinance returned empty data!")
except Exception as e:
    log(f"  yfinance error: {e}")

# Save output
outfile = "bse_api_test_output.txt"
with open(outfile, "w") as f:
    f.write("\n".join(output_lines))
print(f"\nOutput saved to {outfile}")
