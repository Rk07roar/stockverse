"""
StockVest - main.py
FastAPI application entry point.
"""
import os
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from data.fetcher import DataFetcher
from data.cache import Cache
from db import init_db
from api.stocks import router as stocks_router

_optional_routers = []
for mod, prefix, tag in [
    ("api.debug",        "/api/debug",        "debug"),
    ("api.auth",         "/api/auth",         "auth"),
    ("api.screener",     "/api/screener",     "screener"),
    ("api.ml",           "/api/ml",           "ml"),
    ("api.portfolio",    "/api/portfolio",    "portfolio"),
    ("api.backtest",     "/api/backtest",     "backtest"),
    ("api.institutional","/api/institutional","institutional"),
    ("api.signals",      "/api/signals",      "signals"),
    ("api.ws",           "/ws",               "websocket"),
    ("api.intraday",     "/api/intraday",     "intraday"),
    # ── New premium features ──────────────────────────────────
    ("api.options",      "/api/options",      "options"),
    ("api.alerts",       "/api/alerts",       "alerts"),
    ("api.daily_picks",  "/api/daily-picks",  "daily_picks"),
]:
    try:
        import importlib
        m = importlib.import_module(mod)
        _optional_routers.append((prefix, m.router, tag))
    except Exception as e:
        print(f"Could not load {mod}: {e}")


async def _background_alerts():
    """Check all user alerts every 2 minutes during market hours, 15 min off-hours."""
    from data.nse_fetcher import is_market_open
    while True:
        interval = 2 * 60 if is_market_open() else 15 * 60
        await asyncio.sleep(interval)
        try:
            from api.alerts import check_all_alerts
            await check_all_alerts()
        except Exception as e:
            print(f"Alert check failed: {e}")


async def _background_refresh():
    from data.nse_fetcher import fetch_nse_live_quotes, is_market_open
    loop = asyncio.get_event_loop()
    while True:
        interval = 2 * 60 if is_market_open() else 15 * 60
        await asyncio.sleep(interval)
        try:
            from data.fetcher import _sanity_filter
            live = await loop.run_in_executor(None, fetch_nse_live_quotes)
            if live:
                clean = _sanity_filter(live, DataFetcher._real_quotes)
                rejected = len(live) - len(clean)
                if rejected:
                    print(f"Background refresh: {rejected} stocks rejected by sanity check")
                DataFetcher._real_quotes.update(clean)
                DataFetcher._rebuild_stocks()
                DataFetcher._save_price_cache()
                print(f"Market data refreshed: {len(clean)} stocks")
        except Exception as e:
            print(f"Refresh failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await Cache.init()
    print("Initialising database...")
    await init_db()
    print("Loading stock symbols + live quotes (this may take ~30s)...")
    await DataFetcher.load_symbols()
    print(f"{DataFetcher.symbol_count} stocks ready")
    try:
        from ml.trainer import ensure_model_trained
        asyncio.create_task(ensure_model_trained())
        print("ML model training scheduled (background)")
    except Exception as e:
        print(f"ML trainer unavailable: {e}")
    async def _prewarm():
        try:
            import importlib
            inst = importlib.import_module("api.institutional")
            await inst.get_live_institutional()
            print("Institutional cache pre-warmed")
        except Exception as e:
            print(f"Institutional pre-warm failed: {e}")
    asyncio.create_task(_prewarm())
    task = asyncio.create_task(_background_refresh())
    alert_task = asyncio.create_task(_background_alerts())
    yield
    task.cancel()
    alert_task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title="StockVest API",
    version="2.0.0",
    description="Indian stock market intelligence platform",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(stocks_router, prefix="/api/stocks", tags=["stocks"])
for prefix, router, tag in _optional_routers:
    app.include_router(router, prefix=prefix, tags=[tag])

FRONTEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend"))

if os.path.isdir(FRONTEND_DIR):
    @app.get("/", include_in_schema=False)
    async def serve_root():
        return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
    app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="static")
