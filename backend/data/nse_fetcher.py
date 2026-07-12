"""
StockVest — data/nse_fetcher.py
Official NSE and BSE data fetchers. No bulk rate limits.

Sources:
  NSE Bhav Copy   : nsearchives.nseindia.com — all NSE EQ stocks EOD in one ZIP
  NSE Live Market : www.nseindia.com/api     — real-time Nifty index constituents
  BSE Bhav Copy   : www.bseindia.com         — all BSE EQ stocks EOD in one ZIP

Tries multiple URL formats (NSE/BSE change their paths occasionally).
"""
import csv
import io
import logging
import time
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Dict

import requests

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# Hard price bounds for any Indian equity — anything outside is a data error
_MIN_PRICE = 0.05       # below 5 paise = delisted / data garbage
_MAX_PRICE = 200_000    # no equity trades above ₹2 lakh

def _price_ok(price: float) -> bool:
    """Return True if price is within the hard sanity bounds."""
    try:
        return _MIN_PRICE <= float(price) <= _MAX_PRICE
    except (TypeError, ValueError):
        return False

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ── Helpers ────────────────────────────────────────────────────

def is_market_open() -> bool:
    """True if NSE is currently trading (9:15–15:30 IST, Mon–Fri)."""
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    return (now.replace(hour=9, minute=15, second=0, microsecond=0) <= now <=
            now.replace(hour=15, minute=30, second=0, microsecond=0))


def _prev_trading_days(n: int = 6):
    """Yield the last n weekdays (most recent first)."""
    d = datetime.now(IST)
    count = 0
    while count < n:
        if d.weekday() < 5:          # Mon–Fri
            yield d
            count += 1
        d -= timedelta(days=1)


def _is_zip(content: bytes) -> bool:
    return content[:2] == b'PK'


# ── NSE session ────────────────────────────────────────────────

def _make_nse_session() -> requests.Session:
    """
    Build a requests session with NSE cookies.
    NSE's Akamai anti-bot needs: main page visit → brief delay → market page visit.
    """
    s = requests.Session()
    s.headers.update({
        "User-Agent":      _BROWSER_UA,
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection":      "keep-alive",
        "DNT":             "1",
        "Upgrade-Insecure-Requests": "1",
    })
    try:
        # Step 1: hit homepage to get base cookies
        s.get("https://www.nseindia.com/", timeout=12)
        time.sleep(1.5)
        # Step 2: hit the market-data page (sets session-specific cookies)
        s.get("https://www.nseindia.com/market-data/live-equity-market", timeout=12,
              headers={"Referer": "https://www.nseindia.com/"})
        time.sleep(1.0)
    except Exception as e:
        logger.warning(f"NSE session init partial failure: {e}")
    return s


# ── NSE Bhav Copy (all ~1800 NSE EQ stocks EOD) ───────────────

def fetch_nse_bhav_copy() -> Dict[str, dict]:
    """
    Download NSE's daily CM Bhav Copy.
    Returns {SYMBOL: {price, change_pct, open, high, low, volume, mcap, pe}}
    Tries multiple URL formats — NSE has changed the path several times.
    """
    # Build a properly-cookied session for NSE
    session = _make_nse_session()

    api_headers = {
        "Accept":           "application/json, text/plain, */*",
        "Accept-Language":  "en-US,en;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
        "Referer":          "https://www.nseindia.com/market-data/live-equity-market",
    }

    for d in _prev_trading_days(6):
        date_dd_mon_yyyy = d.strftime("%d%b%Y").upper()   # 19JUN2026
        date_yyyymmdd    = d.strftime("%Y%m%d")            # 20260619
        date_dd_mm_yyyy  = d.strftime("%d-%m-%Y")          # 19-06-2026
        month_3          = d.strftime("%b").upper()        # JUN
        year_4           = d.strftime("%Y")                # 2026

        url_candidates = [
            # NSE API endpoint (most reliable, uses proper session)
            (f"https://www.nseindia.com/api/equity-bhavcopy-zip?date={date_dd_mm_yyyy}",
             api_headers),
            # New UDiFF archive format (July 2024+) — old bhavcopy discontinued Jul 8 2024
            (f"https://nsearchives.nseindia.com/content/cm/"
             f"BhavCopy_NSE_CM_0_0_0_{date_yyyymmdd}_F_0000.csv.zip",
             {"Referer": "https://nsearchives.nseindia.com/"}),
            # Alt new format
            (f"https://nsearchives.nseindia.com/products/content/"
             f"sec_bhavdata_full_{d.strftime('%d%m%Y')}.csv",
             {"Referer": "https://nsearchives.nseindia.com/"}),
            # Old format (pre-2023)
            (f"https://nsearchives.nseindia.com/content/historical/EQUITIES/"
             f"{year_4}/{month_3}/cm{date_dd_mon_yyyy}bhav.csv.zip",
             {"Referer": "https://www.nseindia.com/"}),
            # Mirror
            (f"https://archives.nseindia.com/content/historical/EQUITIES/"
             f"{year_4}/{month_3}/cm{date_dd_mon_yyyy}bhav.csv.zip",
             {"Referer": "https://www.nseindia.com/"}),
        ]

        for url, extra_headers in url_candidates:
            try:
                resp = session.get(url, timeout=30, headers=extra_headers)
                if resp.status_code != 200:
                    logger.debug(f"NSE bhav {url[-50:]}: HTTP {resp.status_code}")
                    continue
                content_type = resp.headers.get("Content-Type", "")
                # Handle both ZIP and plain CSV responses
                if _is_zip(resp.content):
                    result = _parse_nse_bhav_zip(resp.content)
                elif (b"SYMBOL" in resp.content[:200] or b"TckrSymb" in resp.content[:200]
                      or "csv" in content_type.lower()):
                    result = _parse_nse_bhav_csv(resp.content)
                else:
                    logger.debug(f"NSE bhav {url[-50:]}: unexpected content type {content_type[:40]}")
                    continue
                if result:
                    logger.info(f"NSE bhav copy {date_dd_mon_yyyy}: {len(result)} stocks ✓")
                    return result
            except Exception as e:
                logger.debug(f"NSE bhav {url[-50:]}: {e}")

    logger.warning("NSE bhav copy: all URL formats failed for last 6 trading days")
    return {}


def _parse_nse_bhav_csv(content: bytes) -> Dict[str, dict]:
    """Parse NSE bhav copy as plain CSV (non-zipped, e.g. sec_bhavdata_full)."""
    result: Dict[str, dict] = {}
    try:
        import io
        reader = csv.DictReader(io.TextIOWrapper(io.BytesIO(content), encoding="utf-8"))
        for row in reader:
            # Old cols: SYMBOL/SERIES  |  New UDiFF (Jul 2024+): TckrSymb/SctySrs
            sym    = (row.get("SYMBOL") or row.get("TckrSymb") or row.get("symbol") or "").strip()
            series = (row.get("SERIES") or row.get("SctySrs")  or row.get("series") or "EQ").strip()
            if series != "EQ" or not sym:
                continue
            try:
                # Old: CLOSE/PREVCLOSE/OPEN/HIGH/LOW/TOTTRDQTY
                # UDiFF: ClsPric/PrvsClsgPric/OpnPric/HghPric/LwPric/TtlTradgVol
                close = float(row.get("CLOSE") or row.get("ClsPric") or row.get("close_price") or 0)
                prev  = float(row.get("PREVCLOSE") or row.get("PrvsClsgPric") or row.get("prev_close") or close)
                if not _price_ok(close):
                    continue
                result[sym] = {
                    "price":      round(close, 2),
                    "change_pct": round((close - prev) / prev * 100, 2) if prev else 0,
                    "open":       round(float(row.get("OPEN") or row.get("OpnPric") or row.get("open_price") or close), 2),
                    "high":       round(float(row.get("HIGH") or row.get("HghPric") or row.get("high_price") or close), 2),
                    "low":        round(float(row.get("LOW")  or row.get("LwPric")  or row.get("low_price")  or close), 2),
                    "volume":     int(float(row.get("TOTTRDQTY") or row.get("TtlTradgVol") or row.get("total_traded_quantity") or 0)),
                    "mcap":       None,
                    "pe":         None,
                }
            except (ValueError, TypeError):
                continue
    except Exception as e:
        logger.warning(f"NSE bhav CSV parse error: {e}")
    return result


def _parse_nse_bhav_zip(content: bytes) -> Dict[str, dict]:
    result: Dict[str, dict] = {}
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
        csv_name = zf.namelist()[0]
        with zf.open(csv_name) as raw_f:
            reader = csv.DictReader(io.TextIOWrapper(raw_f, encoding="utf-8"))
            for row in reader:
                # Old cols: SYMBOL/SERIES  |  New UDiFF (Jul 2024+): TckrSymb/SctySrs
                sym    = (row.get("SYMBOL") or row.get("TckrSymb") or "").strip()
                series = (row.get("SERIES") or row.get("SctySrs")  or "EQ").strip()
                if series != "EQ" or not sym:
                    continue
                try:
                    # Old: CLOSE/PREVCLOSE  |  UDiFF: ClsPric/PrvsClsgPric
                    close = float(row.get("CLOSE")     or row.get("ClsPric")      or 0)
                    prev  = float(row.get("PREVCLOSE") or row.get("PrvsClsgPric") or close)
                    if not _price_ok(close):
                        continue
                    result[sym] = {
                        "price":      round(close, 2),
                        "change_pct": round((close - prev) / prev * 100, 2) if prev else 0,
                        "open":       round(float(row.get("OPEN") or row.get("OpnPric") or close), 2),
                        "high":       round(float(row.get("HIGH") or row.get("HghPric") or close), 2),
                        "low":        round(float(row.get("LOW")  or row.get("LwPric")  or close), 2),
                        "volume":     int(float(row.get("TOTTRDQTY") or row.get("TtlTradgVol") or 0)),
                        "mcap":       None,
                        "pe":         None,
                    }
                except (ValueError, TypeError):
                    continue
    except Exception as e:
        logger.warning(f"NSE bhav ZIP parse error: {e}")
    return result


# ── NSE Live Market (real-time Nifty constituents) ─────────────

_NSE_INDICES = [
    "NIFTY 50",
    "NIFTY NEXT 50",
    "NIFTY 100",
    "NIFTY 200",
    "NIFTY 500",
    "NIFTY MIDCAP 150",
    "NIFTY SMALLCAP 250",
]


def fetch_nse_live_quotes() -> Dict[str, dict]:
    """
    Real-time prices from NSE's own JSON API.
    Requires a session with NSE cookies. Covers ~900 stocks across multiple indices.
    Returns {SYMBOL: {price, change_pct, open, high, low, volume, mcap, pe}}
    """
    import urllib.parse

    session = _make_nse_session()
    api_headers = {
        "Accept":          "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "Referer":         "https://www.nseindia.com/market-data/live-equity-market",
    }

    # Quick connectivity test first
    try:
        test = session.get(
            "https://www.nseindia.com/api/marketStatus",
            timeout=10, headers=api_headers
        )
        if test.status_code != 200:
            logger.warning(f"NSE API unreachable (marketStatus: {test.status_code})")
            return {}
    except Exception as e:
        logger.warning(f"NSE API connectivity test failed: {e}")
        return {}

    result: Dict[str, dict] = {}

    for idx in _NSE_INDICES:
        url = f"https://www.nseindia.com/api/equity-stockIndices?index={urllib.parse.quote(idx)}"
        try:
            resp = session.get(url, timeout=15, headers=api_headers)
            if resp.status_code != 200:
                logger.debug(f"NSE live {idx}: HTTP {resp.status_code}")
                continue
            data = resp.json().get("data", [])
            new  = 0
            for item in data:
                sym = (item.get("symbol") or "").strip()
                if not sym or sym in result:
                    continue
                try:
                    price = float(item.get("lastPrice")     or 0)
                    prev  = float(item.get("previousClose") or price)
                    if price <= 0:
                        continue
                    result[sym] = {
                        "price":      round(price, 2),
                        "change_pct": round((price - prev) / prev * 100, 2) if prev else 0,
                        "open":       round(float(item.get("open",    price) or price), 2),
                        "high":       round(float(item.get("dayHigh", price) or price), 2),
                        "low":        round(float(item.get("dayLow",  price) or price), 2),
                        "volume":     int(float(item.get("totalTradedVolume") or 0)),
                        "mcap":       None,
                        "pe":         None,
                    }
                    new += 1
                except (ValueError, TypeError):
                    continue
            logger.info(f"NSE live {idx}: {new} new quotes")
            time.sleep(0.3)   # brief pause between index calls
        except Exception as e:
            logger.debug(f"NSE live {idx}: {e}")

    logger.info(f"NSE live total: {len(result)} stocks")
    return result


# ── BSE Bhav Copy (all BSE EQ stocks EOD) ─────────────────────

def fetch_bse_bhav_copy() -> Dict[str, dict]:
    """
    Download BSE's daily equity Bhav Copy.
    Returns {BSE_CODE: {price, change_pct, open, high, low, volume, _exchange:'BSE'}}
    """
    headers = {
        "User-Agent": _BROWSER_UA,
        "Referer":    "https://www.bseindia.com/",
        "Accept":     "*/*",
    }
    session = requests.Session()
    session.headers.update(headers)
    # Establish BSE session: homepage + BhavCopy page for correct cookies & referer
    _BSE_BHAV_PAGE = "https://www.bseindia.com/markets/MarketInfo/BhavCopy.aspx"
    try:
        session.get("https://www.bseindia.com/", timeout=10)
        time.sleep(0.5)
        session.get(_BSE_BHAV_PAGE, timeout=10,
                    headers={"Referer": "https://www.bseindia.com/"})
        time.sleep(0.3)
    except Exception:
        pass
    _bse_dl_headers = {**headers, "Referer": _BSE_BHAV_PAGE}

    for d in _prev_trading_days(5):
        ddmmyy   = d.strftime("%d%m%y")         # 170626
        ddmmyyyy = d.strftime("%d%m%Y")          # 17062026
        yyyymmdd = d.strftime("%Y%m%d")          # 20260617

        url_candidates = [
            # BSE UDiFF format (new format, introduced ~2024, mirrors NSE UDiFF)
            f"https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_{yyyymmdd}_F_0000.csv.zip",
            # ISIN code format (mid-2023 to present)
            f"https://www.bseindia.com/download/BhavCopy/Equity/EQ_ISINCODE_{yyyymmdd}.zip",
            # Standard 6-digit date format (DDMMYY) - classic format
            f"https://www.bseindia.com/download/BhavCopy/Equity/EQ{ddmmyy}_CSV.ZIP",
            # 8-digit date format (DDMMYYYY)
            f"https://www.bseindia.com/download/BhavCopy/Equity/EQ{ddmmyyyy}_CSV.ZIP",
            # BSE archives mirror (6-digit)
            f"https://archives.bseindia.com/download/BhavCopy/Equity/EQ{ddmmyy}_CSV.ZIP",
            # BSE API-style endpoint (may return ZIP or JSON redirect)
            f"https://api.bseindia.com/BseIndiaAPI/api/BhavCopyAll/w?stre=&ddlchart=EQ&strdate={yyyymmdd}",
        ]

        for url in url_candidates:
            try:
                resp = session.get(url, timeout=30, headers=_bse_dl_headers)
                if resp.status_code != 200:
                    logger.info(f"BSE bhav {url[-50:]}: HTTP {resp.status_code}")
                    continue
                content = resp.content
                # Direct ZIP response
                if _is_zip(content):
                    result = _parse_bse_bhav_zip(content)
                    if result:
                        logger.info(f"BSE bhav copy {ddmmyy}: {len(result)} stocks ✓ ({url[-50:]})")
                        return result
                    else:
                        logger.info(f"BSE bhav {url[-50:]}: ZIP parsed but 0 stocks — wrong format?")
                    continue
                # Plain CSV response
                if b"SC_CODE" in content[:500] or b"CLOSE" in content[:500] or b"ClsPric" in content[:500]:
                    result = _parse_bse_bhav_csv(content)
                    if result:
                        logger.info(f"BSE bhav copy {ddmmyy}: {len(result)} stocks (CSV) ✓")
                        return result
                    continue
                # JSON response — may contain a download URL
                ct = resp.headers.get("Content-Type", "")
                if "json" in ct:
                    try:
                        jdata = resp.json()
                        dl_url = (jdata.get("url") or jdata.get("downloadUrl")
                                  or jdata.get("fileUrl") or jdata.get("link") or "")
                        if dl_url:
                            r2 = session.get(dl_url, timeout=30, headers=_bse_dl_headers)
                            if r2.status_code == 200 and _is_zip(r2.content):
                                result = _parse_bse_bhav_zip(r2.content)
                                if result:
                                    logger.info(f"BSE bhav copy {ddmmyy}: {len(result)} stocks (JSON→ZIP) ✓")
                                    return result
                    except Exception:
                        pass
                logger.info(f"BSE bhav {url[-50:]}: unrecognised response "
                            f"(CT={ct[:30]}, first bytes={content[:60]!r})")
            except Exception as e:
                logger.info(f"BSE bhav {url[-50:]}: {e}")

    logger.warning("BSE bhav copy: all URL formats failed for last 5 trading days")
    return {}


def _parse_bse_bhav_row(row: dict) -> tuple:
    """
    Parse one row from a BSE bhav copy CSV (supports old format AND UDiFF format).
    Returns (code_str, quote_dict) or (None, None) if row should be skipped.

    Old format columns:  SC_CODE, SC_NAME, OPEN, HIGH, LOW, CLOSE, PREVCLOSE, NO_OF_SHRS
    UDiFF format cols:   FinInstrmId / SC_CODE, TckrSymb, OpnPric, HghPric, LwPric,
                         ClsPric, PrvsClsgPric, TtlTradgVol
    """
    # BSE scrip code — try all known column names
    code = (row.get("SC_CODE") or row.get("FinInstrmId") or row.get("SCRIP_CD")
            or row.get("scripcd") or "").strip()
    if not code:
        return None, None

    # Series filter — skip non-equity rows when series column is present
    series = (row.get("SC_TYPE") or row.get("SctySrs") or row.get("SERIES") or "EQ").strip().upper()
    if series not in ("EQ", "BE", "BZ", "A", "B", "T", "S", "XT", "Z", ""):
        return None, None

    try:
        close = float(
            row.get("CLOSE") or row.get("ClsPric") or row.get("close") or 0
        )
        prev = float(
            row.get("PREVCLOSE") or row.get("PrvsClsgPric") or row.get("prev_close") or close
        )
        if not _price_ok(close):
            return None, None

        name = (row.get("SC_NAME") or row.get("TckrSymb") or row.get("FinInstrmNm")
                or row.get("Scrip_Name") or "").strip().title()

        quote = {
            "price":      round(close, 2),
            "change_pct": round((close - prev) / prev * 100, 2) if prev else 0,
            "open":       round(float(row.get("OPEN") or row.get("OpnPric") or close), 2),
            "high":       round(float(row.get("HIGH") or row.get("HghPric") or close), 2),
            "low":        round(float(row.get("LOW")  or row.get("LwPric")  or close), 2),
            "volume":     int(float(row.get("NO_OF_SHRS") or row.get("TtlTradgVol")
                                    or row.get("TRDQTY") or 0)),
            "mcap":       None,
            "pe":         None,
            "_exchange":  "BSE",
            "_name":      name,
        }
        return str(code), quote
    except (ValueError, TypeError):
        return None, None


def _parse_bse_bhav_zip(content: bytes) -> Dict[str, dict]:
    result: Dict[str, dict] = {}
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
        # Some ZIPs contain multiple files; pick the first CSV
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        csv_name = csv_names[0] if csv_names else zf.namelist()[0]
        with zf.open(csv_name) as raw_f:
            reader = csv.DictReader(io.TextIOWrapper(raw_f, encoding="utf-8", errors="replace"))
            for row in reader:
                code, quote = _parse_bse_bhav_row(row)
                if code:
                    result[code] = quote
        if not result:
            logger.info(f"BSE bhav ZIP: parsed 0 rows from {csv_name} — "
                        f"columns found: {list(reader.fieldnames or [])[:10]}")
    except Exception as e:
        logger.warning(f"BSE bhav ZIP parse error: {e}")
    return result


def _parse_bse_bhav_csv(content: bytes) -> Dict[str, dict]:
    """Parse BSE bhav copy delivered as plain CSV (not zipped)."""
    result: Dict[str, dict] = {}
    try:
        reader = csv.DictReader(io.TextIOWrapper(io.BytesIO(content), encoding="utf-8", errors="replace"))
        for row in reader:
            code, quote = _parse_bse_bhav_row(row)
            if code:
                result[code] = quote
    except Exception as e:
        logger.warning(f"BSE bhav CSV parse error: {e}")
    return result


def fetch_bse_prices_per_stock(bse_codes: list, max_workers: int = 30) -> dict:
    """
    Fetch prices for BSE stocks via BSE's per-stock JSON API.
    Uses concurrent.futures.ThreadPoolExecutor for parallel requests.

    This bypasses the bhav copy authentication wall by calling individual
    stock endpoints which don't require JavaScript-based session tokens.

    Returns {BSE_CODE: {price, change_pct, open, high, low, volume, _exchange:'BSE'}}
    """
    import concurrent.futures

    if not bse_codes:
        return {}

    headers = {
        "User-Agent": _BROWSER_UA,
        "Referer":    "https://www.bseindia.com/",
        "Accept":     "application/json, text/plain, */*",
        "Origin":     "https://www.bseindia.com",
        "X-Requested-With": "XMLHttpRequest",
    }

    # API URL templates to try in order (first working one wins per stock)
    API_TEMPLATES = [
        "https://api.bseindia.com/BseIndiaAPI/api/getScripHeaderData/w?scripcode={code}",
        "https://api.bseindia.com/BseIndiaAPI/api/ComHeader/w?quotetype=EQ&scripcode={code}",
        "https://api.bseindia.com/BseIndiaAPI/api/marketWatch/w?scripcode={code}&seriesid=EQ",
        "https://api.bseindia.com/BseIndiaAPI/api/getQuotesScrData/w?scripcode={code}&group=A",
    ]

    # Cap workers to avoid triggering BSE WAF/rate-limit (403)
    max_workers = min(max_workers, 10)

    # Session with BSE cookies
    session = requests.Session()
    session.headers.update(headers)
    try:
        session.get("https://www.bseindia.com/", timeout=10)
        time.sleep(1.0)  # Give BSE a moment before bulk requests
    except Exception:
        pass

    result = {}
    error_count = 0

    def _fetch_one(code: str):
        for url_tpl in API_TEMPLATES:
            url = url_tpl.format(code=code)
            try:
                r = session.get(url, timeout=8, headers=headers)
                if r.status_code != 200:
                    continue
                ct = r.headers.get("Content-Type", "")
                if "json" not in ct.lower():
                    # Not JSON — skip if it looks like HTML
                    if b"<!DOCTYPE" in r.content[:50] or b"<html" in r.content[:50]:
                        continue
                try:
                    data = r.json()
                except Exception:
                    continue

                if isinstance(data, list) and data:
                    data = data[0]  # Some endpoints return a list

                if not isinstance(data, dict):
                    continue

                # Try every known field name for closing price
                # Handle nested CurrRate dict: {"LTP":"1326.55","Chg":"+17.20","PcChg":"+1.31",...}
                curr_rate = data.get("CurrRate")
                chg_pct_direct = None
                if isinstance(curr_rate, dict):
                    close_raw = curr_rate.get("LTP") or curr_rate.get("ltp") or 0
                    try:
                        chg_pct_direct = float(str(curr_rate.get("PcChg") or "0").replace(",", "").replace("+", ""))
                    except Exception:
                        chg_pct_direct = None
                else:
                    close_raw = (
                        data.get("CurrPric") or data.get("close") or data.get("Close")
                        or curr_rate or data.get("LTP") or data.get("ltp")
                        or data.get("LastTradedPrice") or data.get("last_price")
                        or data.get("PrevClos") or 0
                    )
                prev_raw = (
                    data.get("PrevClos") or data.get("prevclose") or data.get("Prevclose")
                    or data.get("PreviousClose") or data.get("prev_close")
                    or data.get("PrvsClsgPric") or close_raw or 0
                )

                try:
                    close = float(str(close_raw).replace(",", ""))
                    prev  = float(str(prev_raw).replace(",", "") or close)
                except (ValueError, TypeError):
                    continue

                if not _price_ok(close):
                    continue

                # Volume
                vol_raw = (
                    data.get("TotalNoofSharesTraded") or data.get("Volume") or data.get("volume")
                    or data.get("TtlTradgVol") or data.get("NO_OF_SHRS") or 0
                )
                try:
                    vol = int(float(str(vol_raw).replace(",", "")))
                except (ValueError, TypeError):
                    vol = 0

                if chg_pct_direct is not None:
                    change_pct = round(chg_pct_direct, 2)
                else:
                    change_pct = round((close - prev) / prev * 100, 2) if prev else 0
                return code, {
                    "price":      round(close, 2),
                    "change_pct": change_pct,
                    "open":       round(float(str(data.get("Open") or data.get("open") or data.get("OpnPric") or close).replace(",", "")), 2),
                    "high":       round(float(str(data.get("High") or data.get("high") or data.get("HghPric") or close).replace(",", "")), 2),
                    "low":        round(float(str(data.get("Low")  or data.get("low")  or data.get("LwPric")  or close).replace(",", "")), 2),
                    "volume":     vol,
                    "mcap":       None,
                    "pe":         None,
                    "_exchange":  "BSE",
                }
            except Exception:
                continue
        return code, None

    logger.info(f"BSE per-stock API: fetching {len(bse_codes)} stocks with {max_workers} workers...")
    t0 = time.time()

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_fetch_one, code): code for code in bse_codes}
        done = 0
        for future in concurrent.futures.as_completed(futures):
            try:
                code, quote = future.result(timeout=15)
                if quote:
                    result[code] = quote
                else:
                    error_count += 1
            except Exception:
                error_count += 1
            done += 1
            if done % 200 == 0:
                logger.info(f"BSE per-stock API: {done}/{len(bse_codes)} done, {len(result)} with prices")

    elapsed = time.time() - t0
    logger.info(
        f"BSE per-stock API: {len(result)}/{len(bse_codes)} stocks with prices "
        f"({error_count} errors) in {elapsed:.1f}s"
    )
    return result


def fetch_bse_prices_by_group() -> dict:
    """
    Fetch BSE prices in BULK by requesting each market group.
    BSE groups: A, B, F, G, M, S, T, X, XT, XD, Z, SM, IP, etc.
    Each group API call returns ALL stocks in that group simultaneously.
    ~15-20 requests instead of 5000+ per-stock = no rate-limit / WAF issues!

    Returns {BSE_CODE: {price, change_pct, open, high, low, volume, _exchange:'BSE'}}
    """
    # BSE equity market groups — covers all BSE-listed stocks
    BSE_GROUPS = ["A", "B", "F", "G", "M", "S", "T", "X", "XT", "XD", "Z",
                  "IP", "SM", "SY", "IV", "BE", "BZ", "BO", "IF", "N", "R", "W"]

    headers = {
        "User-Agent": _BROWSER_UA,
        "Referer":    "https://www.bseindia.com/",
        "Accept":     "application/json, text/plain, */*",
        "Origin":     "https://www.bseindia.com",
        "X-Requested-With": "XMLHttpRequest",
    }

    session = requests.Session()
    session.headers.update(headers)
    try:
        session.get("https://www.bseindia.com/", timeout=10)
        time.sleep(0.5)
    except Exception:
        pass

    result: Dict[str, dict] = {}
    groups_ok = 0

    for group in BSE_GROUPS:
        try:
            url = (f"https://api.bseindia.com/BseIndiaAPI/api/"
                   f"GetLatestStockPricedataByGroupNew/w?Group={group}")
            r = session.get(url, timeout=20, headers=headers)
            if r.status_code == 403:
                logger.warning(f"BSE group API: HTTP 403 on group {group} — rate limited, stopping")
                break
            if r.status_code != 200:
                logger.info(f"BSE group {group}: HTTP {r.status_code} — skip")
                continue
            try:
                data = r.json()
            except Exception:
                continue

            rows = data if isinstance(data, list) else data.get("Table", data.get("data", []))
            if not rows:
                continue

            group_count = 0
            for row in rows:
                code = str(
                    row.get("scripcode") or row.get("SCRIP_CD") or row.get("ScripCode")
                    or row.get("scripCode") or ""
                ).strip()
                if not code:
                    continue

                # Price extraction — handle nested CurrRate dict or flat fields
                curr_rate = row.get("CurrRate")
                chg_pct_direct = None
                if isinstance(curr_rate, dict):
                    close_raw = curr_rate.get("LTP") or curr_rate.get("ltp") or 0
                    try:
                        chg_pct_direct = float(
                            str(curr_rate.get("PcChg") or "0").replace(",", "").replace("+", ""))
                    except Exception:
                        pass
                else:
                    close_raw = (
                        row.get("CurrPric") or row.get("LTP") or row.get("ltp")
                        or row.get("LastTradedPrice") or curr_rate
                        or row.get("Close") or row.get("CLOSE") or 0
                    )
                try:
                    close = float(str(close_raw).replace(",", ""))
                except (ValueError, TypeError):
                    continue
                if not _price_ok(close):
                    continue

                prev_raw = (row.get("PrevClos") or row.get("prevclose")
                            or row.get("PreviousClose") or close_raw)
                try:
                    prev = float(str(prev_raw).replace(",", "") or close)
                except (ValueError, TypeError):
                    prev = close

                if chg_pct_direct is None:
                    try:
                        chg_str = str(
                            row.get("ChangePer") or row.get("PcChg") or row.get("change_pct") or "")
                        chg_pct_direct = (float(chg_str.replace(",", "").replace("+", ""))
                                          if chg_str else None)
                    except Exception:
                        pass
                change_pct = (round(chg_pct_direct, 2) if chg_pct_direct is not None
                              else round((close - prev) / prev * 100, 2) if prev else 0)

                vol_raw = (row.get("TotalNoofSharesTraded") or row.get("Volume")
                           or row.get("volume") or row.get("TtlTradgVol") or 0)
                try:
                    vol = int(float(str(vol_raw).replace(",", "")))
                except (ValueError, TypeError):
                    vol = 0

                def _sf(keys, default=close):
                    for k in keys:
                        v = row.get(k)
                        if v not in (None, "", "0", 0):
                            try:
                                return round(float(str(v).replace(",", "")), 2)
                            except Exception:
                                pass
                    return round(float(default), 2)

                result[code] = {
                    "price":      round(close, 2),
                    "change_pct": change_pct,
                    "open":  _sf(["Open", "open", "OpnPric"]),
                    "high":  _sf(["High", "high", "HghPric"]),
                    "low":   _sf(["Low",  "low",  "LwPric"]),
                    "volume": vol,
                    "mcap":  None,
                    "pe":    None,
                    "_exchange": "BSE",
                }
                group_count += 1

            if group_count > 0:
                groups_ok += 1
                logger.info(f"BSE group {group}: {group_count} stocks ✓")
            else:
                logger.debug(f"BSE group {group}: 0 stocks (empty/unsupported group)")

            time.sleep(0.3)  # polite delay
        except Exception as e:
            logger.warning(f"BSE group {group}: {e}")

    logger.info(f"BSE group API: {len(result)} total stocks from {groups_ok}/{len(BSE_GROUPS)} groups")
    return result


def fetch_nse_equity_list() -> list:
    """
    Download NSE's official complete equity list (EQUITY_L.csv).
    Returns list of {nse-code, name, isin, bse-code} dicts for all NSE-listed equities.
    Covers ~2400+ NSE stocks with official names.
    """
    url = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
    try:
        session = _make_nse_session()
        resp = session.get(url, timeout=20, headers={
            "Referer": "https://www.nseindia.com/market-data/all-companies",
            "Accept":  "text/csv,text/plain,*/*",
        })
        if resp.status_code != 200:
            logger.warning(f"NSE EQUITY_L.csv: HTTP {resp.status_code}")
            return []
        reader = csv.DictReader(io.StringIO(resp.text))
        result = []
        for row in reader:
            sym    = (row.get("SYMBOL") or "").strip()
            series = (row.get(" SERIES") or row.get("SERIES") or "").strip()
            name   = (row.get(" NAME OF COMPANY") or row.get("NAME OF COMPANY") or sym).strip()
            isin   = (row.get(" ISIN NUMBER") or row.get("ISIN NUMBER") or "").strip()
            if not sym or series not in ("EQ", "BE", "SM", "ST", ""):
                continue
            result.append({"nse-code": sym, "name": name, "isin": isin, "bse-code": ""})
        logger.info(f"NSE EQUITY_L.csv: {len(result)} stocks")
        return result
    except Exception as e:
        logger.warning(f"NSE EQUITY_L.csv fetch failed: {e}")
        return []


def fetch_bse_equity_list() -> list:
    """
    Fetch BSE's complete list of active equity scrips.
    Returns list of {bse-code, name, nse-code} dicts.
    Uses BSE's public JSON API endpoint.
    """
    url = "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w?segment=Equity&status=Active"
    headers = {
        "User-Agent": _BROWSER_UA,
        "Referer":    "https://www.bseindia.com/",
        "Accept":     "application/json, text/plain, */*",
        "Origin":     "https://www.bseindia.com",
    }
    try:
        session = requests.Session()
        session.headers.update(headers)
        # Visit BSE homepage first for cookies
        session.get("https://www.bseindia.com/", timeout=10)
        time.sleep(0.3)
        resp = session.get(url, timeout=20, headers=headers)
        if resp.status_code != 200:
            logger.warning(f"BSE equity list API: HTTP {resp.status_code}")
            return []
        data = resp.json()
        # Response is a list or {Table: [...]}
        rows = data if isinstance(data, list) else data.get("Table", data.get("data", []))
        result = []
        for row in rows:
            code = str(row.get("SCRIP_CD") or row.get("scrip_cd") or "").strip()
            name = (row.get("Scrip_Name") or row.get("scrip_name") or row.get("SCRIP_NAME") or "").strip()
            nse  = (row.get("NSE_SYMBOL") or row.get("nse_symbol") or "").strip()
            isin = (row.get("ISIN_NUMBER") or row.get("isin_number") or row.get("ISIN") or "").strip()
            if not code or not name:
                continue
            result.append({"bse-code": code, "name": name.title(), "nse-code": nse, "isin": isin})
        logger.info(f"BSE equity list API: {len(result)} stocks")
        return result
    except Exception as e:
        logger.warning(f"BSE equity list API failed: {e}")
        return []
