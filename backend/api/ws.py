"""
StockVest — api/ws.py
WebSocket live price feed (free tier — Yahoo Finance 1-min bars, 15-min delayed).

Architecture
------------
• One persistent background loop per server process
• Every 60 s  → fetches fresh 1-min OHLCV from yfinance for all subscribed symbols
              → stores in Cache with 120 s TTL
• Every 3 s   → reads those cache entries and broadcasts to every connected client
              → filters payload so each client only receives its own subscribed symbols

Client protocol
---------------
  Connect  : ws://localhost:8000/ws/prices
  Subscribe: send  {"subscribe": ["RELIANCE", "TCS", "INFY"]}
  Receive  : {"type": "prices",      "data": {"RELIANCE": {"price": 2456.7, "change_pct": 0.52, ...}, ...}, "ts": "14:32:01"}
             {"type": "subscribed",  "symbols": [...], "interval_ms": 3000}
             {"type": "ping"}         ← keep-alive every 30 s if idle
             {"type": "error",        "msg": "..."}
"""
import asyncio
import json
import logging
from datetime import datetime
from typing import Dict, Optional, Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from data.fetcher import DataFetcher
from data.cache import Cache

logger = logging.getLogger(__name__)
router = APIRouter()

_BROADCAST_INTERVAL = 1    # seconds between pushes to clients (reads from cache — fast)
_YF_REFRESH_INTERVAL = 60  # seconds between yfinance fetches (free tier limit)
_KEEPALIVE_TIMEOUT = 30    # seconds of client silence before we send a ping


# ── Connection Manager ────────────────────────────────────────────────────────

class _Manager:
    """Tracks active WebSocket connections and their symbol subscriptions."""

    def __init__(self):
        self._conns: Dict[WebSocket, Set[str]] = {}

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._conns[ws] = set()

    def subscribe(self, ws: WebSocket, symbols: list):
        self._conns[ws] = {s.upper() for s in symbols}

    def disconnect(self, ws: WebSocket):
        self._conns.pop(ws, None)

    def all_symbols(self) -> Set[str]:
        """Union of every client's subscription list."""
        out: Set[str] = set()
        for syms in self._conns.values():
            out.update(syms)
        return out

    @property
    def count(self) -> int:
        return len(self._conns)

    async def broadcast(self, all_prices: dict, ts: str):
        """Send each client only the symbols it subscribed to."""
        dead = []
        for ws, syms in list(self._conns.items()):
            payload = {s: all_prices[s] for s in syms if s in all_prices}
            if not payload:
                continue
            try:
                await ws.send_json({"type": "prices", "data": payload, "ts": ts})
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = _Manager()
_bg_task: Optional[asyncio.Task] = None


# ── yfinance fetch (runs in thread pool) ──────────────────────────────────────

def _yf_fetch_sync(symbols: list) -> dict:
    """
    Download 1-day / 1-minute bars from Yahoo Finance for NSE symbols only.
    Skips BSE-numeric codes (6-digit numbers) — they don't have .NS tickers.
    Returns dict[symbol] = {price, change, change_pct, open, high, low, volume}.
    """
    import yfinance as yf
    import pandas as pd
    import io as _io
    import sys as _sys

    prices: dict = {}

    # Only fetch symbols that are valid NSE tickers (not pure BSE numeric codes)
    nse_symbols = [s for s in symbols if not s.isdigit()]
    if not nse_symbols:
        return prices

    tks = [f"{s}.NS" for s in nse_symbols]

    try:
        # Suppress yfinance "possibly delisted" stderr spam
        _orig_stderr = _sys.stderr
        _sys.stderr = _io.StringIO()
        try:
            raw = yf.download(
                tks,
                period="1d",
                interval="1m",
                group_by="ticker",
                auto_adjust=True,
                progress=False,
                prepost=False,
            )
        finally:
            _sys.stderr = _orig_stderr
        if raw.empty:
            return prices

        for sym, tk in zip(nse_symbols, tks):
            try:
                if isinstance(raw.columns, pd.MultiIndex):
                    if tk not in raw.columns.get_level_values(0):
                        continue
                    df = raw[tk].dropna(how="all")
                else:
                    df = raw.dropna(how="all")

                if df.empty:
                    continue

                price   = float(df["Close"].iloc[-1])
                open_   = float(df["Open"].iloc[0])
                high    = float(df["High"].max())
                low     = float(df["Low"].min())
                vol     = int(df["Volume"].sum())
                change  = round(price - open_, 2)
                chg_pct = round((price / open_ - 1) * 100, 2) if open_ else 0.0

                prices[sym] = {
                    "price":      round(price, 2),
                    "change":     change,
                    "change_pct": chg_pct,
                    "open":       round(open_, 2),
                    "high":       round(high, 2),
                    "low":        round(low, 2),
                    "volume":     vol,
                }
            except Exception as e:
                logger.debug(f"WS yf parse {sym}: {e}")

    except Exception as e:
        logger.debug(f"WS yf download: {e}")

    return prices


# ── Background broadcast loop ─────────────────────────────────────────────────

async def _broadcast_loop():
    """
    Singleton loop:
      • Every 60 s → re-fetch yfinance for all subscribed symbols → cache results
      • Every 3 s  → read cache → broadcast to clients
    """
    loop = asyncio.get_event_loop()
    last_yf_fetch = 0.0

    while True:
        await asyncio.sleep(_BROADCAST_INTERVAL)

        if manager.count == 0:
            continue

        symbols = list(manager.all_symbols())
        if not symbols:
            continue

        now = loop.time()

        # Refresh from yfinance every 60 seconds
        if now - last_yf_fetch >= _YF_REFRESH_INTERVAL:
            try:
                fresh = await loop.run_in_executor(None, _yf_fetch_sync, symbols)
                for sym, data in fresh.items():
                    await Cache.set(f"ws:price:{sym}", data, ttl=120)
                last_yf_fetch = now
                logger.debug(f"WS: refreshed {len(fresh)}/{len(symbols)} symbols from yfinance")
            except Exception as e:
                logger.debug(f"WS yf refresh error: {e}")

        # Build snapshot: DataFetcher._real_quotes (NSE live, 90s) → yfinance cache → skip
        # DataFetcher._real_quotes is the freshest source — updated by NSE live API every 90s.
        # yfinance is 15-min delayed, so only use it for symbols missing from the main feed.
        snapshot: dict = {}
        for sym in symbols:
            # PRIMARY: use the live NSE/BSE quote already in DataFetcher
            rq = DataFetcher._real_quotes.get(sym)
            if rq and rq.get("price", 0) > 0:
                snapshot[sym] = {
                    "price":      rq.get("price", 0),
                    "change_pct": rq.get("change_pct", 0),
                    "open":       rq.get("open", 0),
                    "high":       rq.get("high", 0),
                    "low":        rq.get("low", 0),
                    "volume":     rq.get("volume", 0),
                }
                continue
            # FALLBACK: yfinance cache (15-min delayed) for symbols not in main feed
            entry = await Cache.get(f"ws:price:{sym}")
            if entry:
                snapshot[sym] = entry
            else:
                q = await DataFetcher.get_quote(sym)
                if q:
                    snapshot[sym] = {
                        "price":      q.get("price", 0),
                        "change_pct": q.get("change_pct", 0),
                        "open":       q.get("open", 0),
                        "high":       q.get("high", 0),
                        "low":        q.get("low", 0),
                        "volume":     q.get("volume", 0),
                    }

        if snapshot:
            ts = datetime.now().strftime("%H:%M:%S")
            await manager.broadcast(snapshot, ts)


def _ensure_bg_task():
    global _bg_task
    if _bg_task is None or _bg_task.done():
        _bg_task = asyncio.create_task(_broadcast_loop())
        logger.info("WS: broadcast loop started")


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@router.websocket("/prices")
async def ws_prices(ws: WebSocket):
    """
    Live price WebSocket.

    1. Client connects.
    2. Client sends: {"subscribe": ["RELIANCE", "TCS"]}
    3. Server immediately pushes current cached prices.
    4. Server pushes updates every ~3 s as prices change.
    5. Server sends {"type":"ping"} every 30 s of client silence (keep-alive).
    6. Client can re-subscribe anytime by sending a new {"subscribe": [...]} message.
    """
    await manager.connect(ws)
    _ensure_bg_task()

    try:
        # Step 1: wait for subscribe message
        try:
            raw = await asyncio.wait_for(ws.receive_text(), timeout=15.0)
            msg = json.loads(raw)
        except asyncio.TimeoutError:
            await ws.send_json({"type": "error", "msg": "Timed out waiting for {subscribe: [...]}"})
            return
        except Exception:
            await ws.send_json({"type": "error", "msg": "Invalid JSON"})
            return

        symbols = [s.upper().strip() for s in msg.get("subscribe", []) if s.strip()]
        if not symbols:
            await ws.send_json({"type": "error", "msg": "subscribe list is empty"})
            return

        manager.subscribe(ws, symbols)
        await ws.send_json({"type": "subscribed", "symbols": symbols, "interval_ms": _BROADCAST_INTERVAL * 1000})

        # Step 2: push initial snapshot immediately (from DataFetcher cache — instant)
        snap: dict = {}
        for sym in symbols:
            q = await DataFetcher.get_quote(sym)
            if q:
                snap[sym] = {
                    "price":      q.get("price", 0),
                    "change":     q.get("change", 0),
                    "change_pct": q.get("change_pct", 0),
                    "open":       q.get("open", 0),
                    "high":       q.get("high", 0),
                    "low":        q.get("low", 0),
                    "volume":     q.get("volume", 0),
                }
        if snap:
            await ws.send_json({
                "type": "prices",
                "data": snap,
                "ts":   datetime.now().strftime("%H:%M:%S"),
            })

        # Step 3: keep-alive loop — handle client messages
        while True:
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=_KEEPALIVE_TIMEOUT)
                msg = json.loads(raw)
                if "subscribe" in msg:
                    new_syms = [s.upper().strip() for s in msg["subscribe"] if s.strip()]
                    manager.subscribe(ws, new_syms)
                    await ws.send_json({"type": "subscribed", "symbols": new_syms})
            except asyncio.TimeoutError:
                # Client is silent — send ping to keep connection alive
                await ws.send_json({"type": "ping"})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"WS session error: {e}")
    finally:
        manager.disconnect(ws)
        logger.debug(f"WS: client disconnected. Active: {manager.count}")
