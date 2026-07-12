"""
StockVest — api/debug.py
Diagnostic endpoints to inspect price sources and fix bad data.
"""
import asyncio
import math
import logging
from fastapi import APIRouter
from data.fetcher import DataFetcher

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/price/{symbol}", summary="Show price from every source for a symbol")
async def debug_price(symbol: str):
    """
    Returns what each data source returns for a symbol.
    Useful for diagnosing wrong prices.
    """
    sym = symbol.upper()
    loop = asyncio.get_event_loop()

    # 1. What's in memory right now
    in_memory = DataFetcher._real_quotes.get(sym)

    # 2. yfinance price
    def _yf():
        try:
            import yfinance as yf, io, sys, math
            results = {}
            for suffix in [".NS", ".BO"]:
                try:
                    _orig = sys.stderr; sys.stderr = io.StringIO()
                    df = yf.Ticker(f"{sym}{suffix}").history(period="5d", auto_adjust=True)
                    sys.stderr = _orig
                    if not df.empty:
                        price = float(df.iloc[-1]["Close"])
                        if not math.isnan(price) and price > 0:
                            results[suffix] = round(price, 2)
                except Exception as e:
                    results[suffix] = f"error: {e}"
            return results
        except Exception as e:
            return {"error": str(e)}

    yf_prices = await loop.run_in_executor(None, _yf)

    # 3. NSE individual quote API
    def _nse_quote():
        try:
            from data.nse_fetcher import _make_nse_session
            session = _make_nse_session()
            headers = {
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": "https://www.nseindia.com/",
            }
            r = session.get(
                f"https://www.nseindia.com/api/quote-equity?symbol={sym}",
                timeout=10, headers=headers
            )
            if r.status_code == 200:
                d = r.json()
                pd = d.get("priceInfo", {})
                return {
                    "lastPrice": pd.get("lastPrice"),
                    "previousClose": pd.get("previousClose"),
                    "open": pd.get("open"),
                    "intraDayHighLow": pd.get("intraDayHighLow"),
                    "weekHighLow": pd.get("weekHighLow"),
                }
            return {"error": f"HTTP {r.status_code}"}
        except Exception as e:
            return {"error": str(e)}

    nse_individual = await loop.run_in_executor(None, _nse_quote)

    return {
        "symbol": sym,
        "in_memory_price": in_memory.get("price") if in_memory else None,
        "in_memory_data": in_memory,
        "yfinance": yf_prices,
        "nse_individual_quote": nse_individual,
        "verdict": _verdict(sym, in_memory, yf_prices, nse_individual),
    }


def _verdict(sym, in_memory, yf_prices, nse_quote):
    prices = []
    if in_memory:
        prices.append(("in_memory", in_memory.get("price", 0)))
    for k, v in yf_prices.items():
        if isinstance(v, (int, float)):
            prices.append((f"yfinance{k}", v))
    nse_price = nse_quote.get("lastPrice") if isinstance(nse_quote, dict) else None
    if nse_price:
        prices.append(("nse_direct", nse_price))
    if not prices:
        return "no data from any source"
    # Check if any two sources disagree by >20%
    vals = [p for _, p in prices if p and p > 0]
    if len(vals) < 2:
        return f"only 1 source available: {prices}"
    mn, mx = min(vals), max(vals)
    if mx > 0 and (mx - mn) / mx > 0.20:
        return f"SOURCES DISAGREE — possible bad data. Prices: {prices}"
    return f"sources agree. Prices: {prices}"


@router.post("/fix-price/{symbol}", summary="Force-refresh price from yfinance")
async def fix_price(symbol: str):
    """Force-fetch the correct price from yfinance and inject it into memory."""
    sym = symbol.upper()
    loop = asyncio.get_event_loop()

    def _fetch():
        try:
            import yfinance as yf, io, sys, math
            for suffix in [".NS", ".BO"]:
                try:
                    _orig = sys.stderr; sys.stderr = io.StringIO()
                    df = yf.Ticker(f"{sym}{suffix}").history(period="5d", auto_adjust=True)
                    sys.stderr = _orig
                    if df.empty:
                        continue
                    price = float(df.iloc[-1]["Close"])
                    prev = float(df.iloc[-2]["Close"]) if len(df) > 1 else price
                    if math.isnan(price) or price <= 0:
                        continue
                    return {
                        "price": round(price, 2),
                        "change_pct": round((price - prev) / prev * 100, 2) if prev else 0,
                        "open": round(float(df.iloc[-1].get("Open", price)), 2),
                        "high": round(float(df.iloc[-1].get("High", price)), 2),
                        "low": round(float(df.iloc[-1].get("Low", price)), 2),
                        "volume": int(df.iloc[-1].get("Volume", 0)),
                        "mcap": None, "pe": None,
                        "source": f"yfinance{suffix}",
                    }
                except Exception:
                    continue
        except Exception:
            pass
        return None

    result = await loop.run_in_executor(None, _fetch)
    if result:
        old = DataFetcher._real_quotes.get(sym, {}).get("price")
        DataFetcher._real_quotes[sym] = result
        DataFetcher._rebuild_stocks()
        DataFetcher._save_price_cache()
        return {
            "ok": True,
            "symbol": sym,
            "old_price": old,
            "new_price": result["price"],
            "source": result["source"],
        }
    return {"ok": False, "symbol": sym, "error": "yfinance returned no data"}


@router.get("/cache-status", summary="Show price cache health")
async def cache_status():
    total = len(DataFetcher._real_quotes)
    prices = [v.get("price", 0) for v in DataFetcher._real_quotes.values()]
    suspicious = {k: v.get("price") for k, v in DataFetcher._real_quotes.items()
                  if v.get("price", 0) > 5000}
    return {
        "total_stocks_in_memory": total,
        "min_price": round(min(prices), 2) if prices else 0,
        "max_price": round(max(prices), 2) if prices else 0,
        "stocks_above_5000": suspicious,
        "idea_price": DataFetcher._real_quotes.get("IDEA", {}).get("price"),
    }
