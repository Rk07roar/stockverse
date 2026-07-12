"""
StockVest — api/stocks.py
FastAPI router for stock data endpoints.
"""
import asyncio
import logging
from fastapi import APIRouter, Query, HTTPException
from typing import Optional
from data.fetcher import DataFetcher
from data.cache import Cache

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/", summary="List all stocks with optional filters")
async def get_stocks(
    search: str = Query("", description="Search by name or symbol"),
    exchange: str = Query("", description="Filter: nse | bse | both"),
    sector: str = Query("", description="Filter by sector"),
    sort: str = Query("name", description="Sort key: name | chg_desc | chg_asc | ml_desc | price_desc | vol_desc"),
    page: int = Query(1, ge=1, description="Page number"),
    limit: int = Query(500, ge=1, le=2000, description="Items per page"),
):
    data = await DataFetcher.get_all_stocks(search=search, exchange=exchange, sector=sector, sort=sort)
    total = len(data)
    start = (page - 1) * limit
    end = start + limit
    return {
        "total": total,
        "page": page,
        "limit": limit,
        "stocks": data[start:end],
    }


@router.get("/raw-symbols", summary="Raw NSE+BSE symbol list (proxy for frontend)")
async def get_raw_symbols():
    """Returns raw symbol list already loaded in memory — avoids CORS issues in browser."""
    return DataFetcher._raw_symbols


@router.get("/indices", summary="Market indices")
async def get_indices():
    return await DataFetcher.get_indices()


@router.get("/nifty-history", summary="NIFTY 50 daily closes for chart")
async def nifty_history(period: str = Query("1mo", description="1wk|1mo|3mo|6mo|1y")):
    valid = {"1wk", "1mo", "3mo", "6mo", "1y"}
    if period not in valid:
        period = "1mo"
    cache_key = f"nifty_history:{period}"
    cached = await Cache.get(cache_key)
    if cached:
        return cached

    def _sync():
        import math
        import yfinance as yf
        tk = yf.Ticker("^NSEI")
        df = tk.history(period=period, auto_adjust=True).dropna(subset=["Close"])
        if df.empty:
            return {"dates": [], "closes": [], "highs": [], "lows": []}
        out_d, out_c, out_h, out_l = [], [], [], []
        for idx, row in df.iterrows():
            c = float(row["Close"])
            h = float(row["High"])
            l = float(row["Low"])
            if math.isnan(c) or math.isinf(c):
                continue
            out_d.append(str(idx.date()))
            out_c.append(round(c, 2))
            out_h.append(round(h if not math.isnan(h) else c, 2))
            out_l.append(round(l if not math.isnan(l) else c, 2))
        return {"dates": out_d, "closes": out_c, "highs": out_h, "lows": out_l}

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, _sync)
    await Cache.set(cache_key, result, ttl=300)
    return result


@router.get("/prices", summary="Lightweight price snapshot — all stocks with real data only")
async def get_prices():
    """
    Returns only {symbol: {price, change_pct, volume}} for stocks that have real price data.
    Much lighter than /api/stocks/ — designed for 30-60s frontend polling.
    Cached for 20 seconds.
    """
    cache_key = "stock_prices_snapshot"
    cached = await Cache.get(cache_key)
    if cached:
        return cached
    quotes = DataFetcher._real_quotes
    result = {
        sym: {
            "price":      round(float(q.get("price", 0)), 2),
            "change_pct": round(float(q.get("change_pct", q.get("chg_pct", 0)) or 0), 2),
            "volume":     int(q.get("volume", 0) or 0),
            "open":       round(float(q.get("open", 0) or 0), 2),
            "high":       round(float(q.get("high", 0) or 0), 2),
            "low":        round(float(q.get("low", 0) or 0), 2),
        }
        for sym, q in quotes.items()
        if q.get("price", 0) > 0
    }
    await Cache.set(cache_key, result, ttl=20)
    return result


@router.get("/search", summary="Quick symbol/name search")
async def search_stocks(q: str = Query(..., min_length=1), limit: int = Query(10, ge=1, le=50)):
    results = await DataFetcher.search(q.lower(), limit=limit)
    return {"results": results}


@router.get("/bse-names", summary="BSE code→name map for all BSE-only stocks")
async def get_bse_names():
    """Returns {bse_code: company_name} for every BSE-only stock that has a proper name."""
    name_map = {
        r["bse-code"]: r["name"]
        for r in DataFetcher._raw_symbols
        if r.get("bse-code")
        and not r.get("nse-code")
        and r.get("name")
        and not r["name"].isdigit()
        and r["name"] != r["bse-code"]
    }
    return name_map


@router.get("/{symbol}", summary="Get quote for a single stock")
async def get_quote(symbol: str):
    stock = await DataFetcher.get_quote(symbol.upper())
    if not stock:
        raise HTTPException(status_code=404, detail=f"Symbol '{symbol}' not found")
    return stock


@router.get("/{symbol}/history", summary="Price history")
async def get_history(symbol: str, period: str = Query("1y", description="1d|1w|1m|3m|6m|1y|5y")):
    data = await DataFetcher.get_history(symbol.upper(), period=period)
    if not data:
        raise HTTPException(status_code=404, detail=f"Symbol '{symbol}' not found")
    return data
