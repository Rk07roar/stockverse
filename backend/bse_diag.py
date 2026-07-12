"""
BSE bhav copy diagnostic — run from backend/ folder.
Tests every URL format and prints what each one returns.
Output is also saved to bse_diag_output.txt
"""
import requests, time, zipfile, io, csv, sys

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

output_lines = []
def log(msg):
    print(msg)
    output_lines.append(msg)

from datetime import datetime, timedelta, timezone
IST = timezone(timedelta(hours=5, minutes=30))

def prev_trading_days(n=5):
    d = datetime.now(IST)
    count = 0
    while count < n:
        if d.weekday() < 5:
            yield d
            count += 1
        d -= timedelta(days=1)

headers = {
    "User-Agent": _BROWSER_UA,
    "Referer":    "https://www.bseindia.com/",
    "Accept":     "*/*",
}

log("=== BSE Bhav Copy Diagnostic ===\n")

# First test BSE connectivity
session = requests.Session()
session.headers.update(headers)

log("Step 1: Testing BSE homepage connectivity...")
try:
    r = session.get("https://www.bseindia.com/", timeout=15)
    log(f"  BSE homepage: HTTP {r.status_code}")
    if r.status_code == 200:
        log(f"  Response length: {len(r.content)} bytes")
        log(f"  Content-Type: {r.headers.get('Content-Type','?')}")
        # Set session cookies
        log(f"  Cookies set: {list(session.cookies.keys())}")
    else:
        log(f"  First 200 bytes: {r.content[:200]!r}")
except Exception as e:
    log(f"  FAILED: {e}")

log("")
log("Step 2: Testing BSE BhavCopy page...")
try:
    r = session.get("https://www.bseindia.com/markets/MarketInfo/BhavCopy.aspx", timeout=15,
                    headers={"Referer": "https://www.bseindia.com/"})
    log(f"  BhavCopy page: HTTP {r.status_code}, {len(r.content)} bytes")
except Exception as e:
    log(f"  FAILED: {e}")

log("")
log("Step 3: Testing all download URL formats for last 5 trading days...\n")

for d in prev_trading_days(5):
    ddmmyy   = d.strftime("%d%m%y")
    ddmmyyyy = d.strftime("%d%m%Y")
    yyyymmdd = d.strftime("%Y%m%d")
    log(f"--- Date: {d.strftime('%Y-%m-%d')} ---")

    urls = [
        f"https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_{yyyymmdd}_F_0000.csv.zip",
        f"https://www.bseindia.com/download/BhavCopy/Equity/EQ_ISINCODE_{yyyymmdd}.zip",
        f"https://www.bseindia.com/download/BhavCopy/Equity/EQ{ddmmyy}_CSV.ZIP",
        f"https://archives.bseindia.com/download/BhavCopy/Equity/EQ{ddmmyy}_CSV.ZIP",
        f"https://api.bseindia.com/BseIndiaAPI/api/BhavCopyAll/w?stre=&ddlchart=EQ&strdate={yyyymmdd}",
    ]

    for url in urls:
        try:
            r = session.get(url, timeout=20, headers={
                **headers,
                "Referer": "https://www.bseindia.com/markets/MarketInfo/BhavCopy.aspx"
            })
            is_zip = r.content[:2] == b'PK'
            ct = r.headers.get("Content-Type", "?")[:50]
            log(f"  {r.status_code} | ZIP={is_zip} | CT={ct}")
            log(f"  URL: {url[-70:]}")
            if is_zip:
                try:
                    zf = zipfile.ZipFile(io.BytesIO(r.content))
                    csv_name = zf.namelist()[0]
                    with zf.open(csv_name) as f:
                        reader = csv.DictReader(io.TextIOWrapper(f, encoding='utf-8', errors='replace'))
                        rows = list(reader)
                        log(f"  ** VALID ZIP! {len(rows)} rows. Columns: {list(reader.fieldnames or [])[:8]}")
                        if rows:
                            log(f"  First row sample: {dict(list(rows[0].items())[:5])}")
                except Exception as ze:
                    log(f"  ZIP parse error: {ze}")
            elif r.status_code == 200:
                log(f"  First 150 bytes: {r.content[:150]!r}")
        except Exception as e:
            log(f"  ERROR: {e}")
            log(f"  URL: {url[-70:]}")
        log("")

    log("")

# Save output
outfile = "bse_diag_output.txt"
with open(outfile, "w") as f:
    f.write("\n".join(output_lines))
log(f"\nOutput saved to {outfile}")
