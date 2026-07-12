"""
StockVest — api/portfolio.py
Full portfolio management: buy/sell, holdings, P&L, transactions.
Uses SQLite via db.py. Default user = 'guest' (no auth required for MVP).
"""
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, date
import aiosqlite

from db import DB_PATH
from data.fetcher import DataFetcher
from utils.calculations import xirr, cagr as calc_cagr, sharpe_ratio, max_drawdown

router = APIRouter()

# ── Request models ─────────────────────────────────────────────
class BuyRequest(BaseModel):
    symbol:   str
    qty:      float
    price:    float
    date:     Optional[str] = None
    notes:    Optional[str] = ""
    source:   Optional[str] = "manual"   # "manual" | "market_pick"
    user_id:  Optional[str] = "guest"

class SellRequest(BaseModel):
    symbol:   str
    qty:      float
    price:    float
    date:     Optional[str] = None
    notes:    Optional[str] = ""
    user_id:  Optional[str] = "guest"

class WatchlistRequest(BaseModel):
    symbol:   str
    name:     Optional[str] = ""
    user_id:  Optional[str] = "guest"


# ── Helpers ────────────────────────────────────────────────────
async def _get_live_price(symbol: str) -> float:
    """Fetch live price via yfinance for the symbol; fallback to DataFetcher (simulated)."""
    sym = symbol.upper()
    # Try real yfinance fetch for this specific symbol
    def _yf_fetch():
        try:
            import yfinance as yf
            for suffix in [".NS", ".BO"]:
                tk = yf.Ticker(f"{sym}{suffix}")
                info = tk.fast_info
                price = getattr(info, "last_price", None) or getattr(info, "regular_market_price", None)
                if price and price > 0:
                    return float(price)
        except Exception:
            pass
        return 0.0

    import asyncio
    loop = asyncio.get_event_loop()
    price = await loop.run_in_executor(None, _yf_fetch)
    if price > 0:
        return price

    # Fallback to DataFetcher (real or simulated)
    stock = await DataFetcher.get_quote(sym)
    if stock and stock.get("price", 0) > 0:
        return stock["price"]
    return 0.0


async def _get_stock_name(symbol: str) -> str:
    stock = await DataFetcher.get_quote(symbol.upper())
    return stock["name"] if stock else symbol


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ── Portfolio endpoints ────────────────────────────────────────
@router.get("/", summary="Portfolio overview with live P&L")
async def get_portfolio(user_id: str = Query("guest")):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Aggregate holdings
        rows = await db.execute_fetchall(
            "SELECT symbol, name, SUM(qty) as qty, "
            "COALESCE(SUM(qty*avg_price)/NULLIF(SUM(qty),0), 0) as avg_price, exchange, "
            "MAX(source) as source "
            "FROM holdings WHERE user_id=? GROUP BY symbol HAVING SUM(qty) > 0",
            (user_id,)
        )

    holdings = []
    total_invested = 0.0
    total_value    = 0.0

    for row in rows:
        sym       = row["symbol"]
        qty       = row["qty"]
        avg_price = row["avg_price"]
        live      = await _get_live_price(sym)
        if live == 0:
            live = avg_price  # last resort: no data available

        invested   = qty * avg_price
        cur_value  = qty * live
        pnl        = cur_value - invested
        pnl_pct    = (pnl / invested * 100) if invested else 0

        total_invested += invested
        total_value    += cur_value

        holdings.append({
            "symbol":     sym,
            "name":       row["name"],
            "exchange":   row["exchange"],
            "qty":        round(qty, 4),
            "avg_price":  round(avg_price, 2),
            "live_price": round(live, 2),
            "invested":   round(invested, 2),
            "cur_value":  round(cur_value, 2),
            "pnl":        round(pnl, 2),
            "pnl_pct":    round(pnl_pct, 2),
            "source":     row["source"] or "manual",
        })

    total_pnl     = total_value - total_invested
    total_pnl_pct = (total_pnl / total_invested * 100) if total_invested else 0

    return {
        "holdings":        holdings,
        "total_invested":  round(total_invested, 2),
        "total_value":     round(total_value, 2),
        "total_pnl":       round(total_pnl, 2),
        "total_pnl_pct":   round(total_pnl_pct, 2),
        "count":           len(holdings),
    }


@router.post("/buy", summary="Add a buy transaction")
async def buy_stock(req: BuyRequest):
    if req.qty <= 0:
        raise HTTPException(400, "Quantity must be positive")
    if req.price <= 0:
        raise HTTPException(400, "Price must be positive")

    sym  = req.symbol.upper()
    name = await _get_stock_name(sym)
    dt   = req.date or _now()

    src = req.source or "manual"

    async with aiosqlite.connect(DB_PATH) as db:
        # Add to holdings
        await db.execute(
            "INSERT INTO holdings (user_id, symbol, name, exchange, qty, avg_price, buy_date, notes, source) "
            "VALUES (?, ?, ?, 'NSE', ?, ?, ?, ?, ?)",
            (req.user_id, sym, name, req.qty, req.price, dt, req.notes or "", src)
        )
        # Record transaction
        await db.execute(
            "INSERT INTO transactions (user_id, symbol, name, action, qty, price, total, date, notes, source) "
            "VALUES (?, ?, ?, 'BUY', ?, ?, ?, ?, ?, ?)",
            (req.user_id, sym, name, req.qty, req.price,
             round(req.qty * req.price, 2), dt, req.notes or "", src)
        )
        await db.commit()

    return {
        "status":  "ok",
        "message": f"Bought {req.qty} shares of {sym} @ ₹{req.price}",
        "symbol":  sym,
        "name":    name,
        "total":   round(req.qty * req.price, 2),
    }


@router.post("/sell", summary="Record a sell transaction")
async def sell_stock(req: SellRequest):
    if req.qty <= 0:
        raise HTTPException(400, "Quantity must be positive")
    if req.price <= 0:
        raise HTTPException(400, "Price must be positive")

    sym = req.symbol.upper()
    dt  = req.date or _now()

    # Check available qty
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT SUM(qty) as total_qty FROM holdings WHERE user_id=? AND symbol=?",
            (req.user_id, sym)
        )).fetchone()
        held = float(row["total_qty"] or 0)
        if held < req.qty:
            raise HTTPException(400, f"Only {held} shares held, cannot sell {req.qty}")

        name = await _get_stock_name(sym)
        # Insert negative holding row to reduce position
        await db.execute(
            "INSERT INTO holdings (user_id, symbol, name, exchange, qty, avg_price, buy_date, notes) "
            "VALUES (?, ?, ?, 'NSE', ?, ?, ?, ?)",
            (req.user_id, sym, name, -req.qty, req.price, dt, req.notes or "")
        )
        await db.execute(
            "INSERT INTO transactions (user_id, symbol, name, action, qty, price, total, date, notes) "
            "VALUES (?, ?, ?, 'SELL', ?, ?, ?, ?, ?)",
            (req.user_id, sym, name, req.qty, req.price,
             round(req.qty * req.price, 2), dt, req.notes or "")
        )
        await db.commit()

    return {
        "status":  "ok",
        "message": f"Sold {req.qty} shares of {sym} @ ₹{req.price}",
        "symbol":  sym,
        "total":   round(req.qty * req.price, 2),
    }


@router.get("/transactions", summary="Full transaction history")
async def get_transactions(user_id: str = Query("guest"), limit: int = Query(100)):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT * FROM transactions WHERE user_id=? ORDER BY date DESC, id DESC LIMIT ?",
            (user_id, limit)
        )
    return {"transactions": [dict(r) for r in rows]}


@router.delete("/holding/{symbol}", summary="Delete all holdings for a symbol")
async def delete_holding(symbol: str, user_id: str = Query("guest")):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM holdings WHERE user_id=? AND symbol=?",
            (user_id, symbol.upper())
        )
        await db.commit()
    return {"status": "ok", "message": f"Removed {symbol.upper()} from portfolio"}


@router.get("/stats", summary="Portfolio statistics with XIRR, Sharpe, drawdown")
async def get_portfolio_stats(user_id: str = Query("guest")):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        tx_rows = await db.execute_fetchall(
            "SELECT * FROM transactions WHERE user_id=? ORDER BY date",
            (user_id,)
        )
    if not tx_rows:
        return {"total_trades": 0, "xirr": 0, "win_rate": 0, "realised_pnl": 0}

    buys  = [r for r in tx_rows if r["action"] == "BUY"]
    sells = [r for r in tx_rows if r["action"] == "SELL"]

    total_invested = sum(float(r["total"]) for r in buys)
    total_realised = sum(float(r["total"]) for r in sells)

    # XIRR cashflows: buys are negative (cash out), sells positive (cash in)
    # Add current portfolio value as final positive cashflow
    portfolio = await get_portfolio(user_id=user_id)
    cur_value = portfolio["total_value"]

    from datetime import date as _date
    cashflows = []
    for r in tx_rows:
        try:
            d   = _date.fromisoformat(r["date"])
            amt = float(r["total"]) * (-1 if r["action"] == "BUY" else 1)
            cashflows.append((d, amt))
        except Exception:
            pass
    # Add current value as liquidation at today
    if cashflows:
        cashflows.append((_date.today(), cur_value))

    xirr_val = 0.0
    try:
        xirr_val = round(xirr(cashflows) * 100, 2)
    except Exception:
        pass

    # Realised P&L: for each sell find matched buy cost
    realised_pnl = 0.0
    buy_costs: dict = {}
    for r in tx_rows:
        sym = r["symbol"]
        qty = float(r["qty"])
        price = float(r["price"])
        if r["action"] == "BUY":
            buy_costs.setdefault(sym, []).append((qty, price))
        else:
            # FIFO cost matching
            cost = 0.0
            remaining = qty
            for bq, bp in buy_costs.get(sym, []):
                if remaining <= 0:
                    break
                used = min(remaining, bq)
                cost += used * bp
                remaining -= used
            realised_pnl += (price * qty) - cost

    # Win rate: % of sell trades profitable
    win_trades = 0
    for r in [r for r in tx_rows if r["action"] == "SELL"]:
        sym   = r["symbol"]
        price = float(r["price"])
        matched_cost = sum(bp for bq, bp in buy_costs.get(sym, [])[:1]) if buy_costs.get(sym) else price
        if price > matched_cost:
            win_trades += 1
    win_rate = round(win_trades / len(sells) * 100, 1) if sells else 0.0

    return {
        "total_trades":   len(tx_rows),
        "total_buys":     len(buys),
        "total_sells":    len(sells),
        "total_invested": round(total_invested, 2),
        "total_realised": round(total_realised, 2),
        "realised_pnl":   round(realised_pnl, 2),
        "current_value":  round(cur_value, 2),
        "xirr":           xirr_val,
        "win_rate":       win_rate,
    }


# ── Realised P&L (FIFO) ────────────────────────────────────────
@router.get("/realised", summary="Realised P&L per symbol via FIFO cost basis")
async def get_realised_pnl(user_id: str = Query("guest")):
    """
    Returns realised gains/losses from closed sell trades.
    Uses strict FIFO matching: oldest buy lots consumed first.
    """
    from collections import deque

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT symbol, action, qty, price, date FROM transactions "
            "WHERE user_id=? ORDER BY date ASC, id ASC",
            (user_id,)
        )

    lots: dict = {}        # {sym: deque of [qty, price]}
    realised: dict = {}    # {sym: float pnl}

    for row in rows:
        sym    = row["symbol"]
        action = row["action"]
        qty    = float(row["qty"])
        price  = float(row["price"])

        if sym not in lots:
            lots[sym]     = deque()
            realised[sym] = 0.0

        if action == "BUY":
            lots[sym].append([qty, price])
        elif action == "SELL":
            remaining = qty
            while remaining > 0 and lots[sym]:
                lot_qty, lot_price = lots[sym][0]
                used = min(lot_qty, remaining)
                realised[sym] += used * (price - lot_price)
                remaining     -= used
                if used >= lot_qty:
                    lots[sym].popleft()
                else:
                    lots[sym][0][0] -= used

    total = sum(realised.values())
    by_symbol = sorted(
        [{"symbol": s, "pnl": round(p, 2), "up": p >= 0}
         for s, p in realised.items() if abs(p) >= 0.01],
        key=lambda x: abs(x["pnl"]), reverse=True
    )
    return {
        "total_realised": round(total, 2),
        "count":          len(by_symbol),
        "by_symbol":      by_symbol,
    }


# ── Watchlist ──────────────────────────────────────────────────
@router.get("/watchlist", summary="Get watchlist")
async def get_watchlist(user_id: str = Query("guest")):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT * FROM watchlist WHERE user_id=? ORDER BY added_at DESC",
            (user_id,)
        )
    # Enrich with live prices
    result = []
    for row in rows:
        sym   = row["symbol"]
        stock = await DataFetcher.get_quote(sym)
        result.append({
            "symbol":     sym,
            "name":       row["name"],
            "added_at":   row["added_at"],
            "price":      stock["price"] if stock else 0,
            "change_pct": stock["change_pct"] if stock else 0,
            "ml_score":   stock["ml_score"] if stock else 0,
        })
    return {"watchlist": result}


@router.post("/watchlist", summary="Add to watchlist")
async def add_watchlist(req: WatchlistRequest):
    sym  = req.symbol.upper()
    name = req.name or (await _get_stock_name(sym))
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT OR IGNORE INTO watchlist (user_id, symbol, name, added_at) VALUES (?,?,?,?)",
                (req.user_id, sym, name, _now())
            )
            await db.commit()
        except Exception as e:
            raise HTTPException(400, str(e))
    return {"status": "ok", "symbol": sym}


@router.delete("/watchlist/{symbol}", summary="Remove from watchlist")
async def remove_watchlist(symbol: str, user_id: str = Query("guest")):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM watchlist WHERE user_id=? AND symbol=?",
            (user_id, symbol.upper())
        )
        await db.commit()
    return {"status": "ok"}
