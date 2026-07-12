"""
StockVest — ml/trainer.py
Trains the GradientBoosting model on Nifty50 historical data.
Called once on startup (or when saved_model.pkl is missing/stale).

Usage:
    from ml.trainer import ensure_model_trained, get_ml_prediction
"""
import asyncio
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)
MODEL_PATH = os.path.join(os.path.dirname(__file__), "saved_model.pkl")
_MODEL_STALE_DAYS = 7   # retrain if pkl older than 7 days


def _is_stale() -> bool:
    if not os.path.exists(MODEL_PATH):
        return True
    import time
    age_days = (time.time() - os.path.getmtime(MODEL_PATH)) / 86400
    return age_days > _MODEL_STALE_DAYS


def _train_sync():
    """Download Nifty50 history and train the model. Runs in thread pool."""
    try:
        import yfinance as yf
        import pandas as pd
        from ml.model import StockMLModel, build_features, build_target

        NIFTY50 = [
            "RELIANCE","TCS","HDFCBANK","ICICIBANK","INFY","HINDUNILVR","ITC",
            "KOTAKBANK","AXISBANK","LT","SBIN","BAJFINANCE","BHARTIARTL",
            "ASIANPAINT","MARUTI","TITAN","SUNPHARMA","ULTRACEMCO","WIPRO",
            "NTPC","ONGC","POWERGRID","M&M","TATAMOTORS","NESTLEIND","TECHM",
            "HCLTECH","BPCL","COALINDIA","HEROMOTOCO","DIVISLAB","DRREDDY",
            "EICHERMOT","CIPLA","ADANIENT","JSWSTEEL","GRASIM","TATASTEEL",
            "HINDALCO","BAJAJFINSV","SBILIFE","HDFCLIFE","APOLLOHOSP",
            "ADANIPORTS","INDUSINDBK","BRITANNIA","SHREECEM","TATACONSUM",
            "UPL","ZOMATO",
        ]
        tickers = [f"{s}.NS" for s in NIFTY50]
        logger.info(f"Downloading 5y data for {len(tickers)} Nifty50 stocks...")
        raw = yf.download(tickers, period="5y", interval="1d",
                          group_by="ticker", auto_adjust=True, progress=False)
        if raw.empty:
            logger.warning("Training data download failed — skipping model training")
            return None

        # Build (X, y) per-stock and align BEFORE concatenating.
        # Concatenating first then calling model.train(combined) fails because
        # duplicate dates across stocks break X.index.intersection(y.index):
        # the intersection returns the union of unique dates, then .loc selects
        # ALL rows matching those dates — giving different counts for X vs y.
        all_X, all_y = [], []
        for sym, ticker in zip(NIFTY50, tickers):
            try:
                if isinstance(raw.columns, pd.MultiIndex):
                    if ticker not in raw.columns.get_level_values(0):
                        continue
                    df = raw[ticker].dropna(how="all").copy()
                else:
                    df = raw.dropna(how="all").copy()

                df.columns = [c.lower() for c in df.columns]
                if len(df) < 252:
                    continue

                X = build_features(df)   # already calls .dropna() internally
                y = build_target(df)     # shift(-30) → NaN at tail

                # Align per-stock (no duplicate dates within a single stock)
                common = X.index.intersection(y.index)
                X = X.loc[common]
                y = y.loc[common]

                if len(X) < 100:
                    continue

                # Reset index so pd.concat doesn't produce duplicate date indices
                all_X.append(X.reset_index(drop=True))
                all_y.append(y.reset_index(drop=True))

            except Exception as e:
                logger.debug(f"Skipping {sym}: {e}")

        if not all_X:
            logger.warning("No usable training frames — skipping model training")
            return None

        X_combined = pd.concat(all_X, ignore_index=True)
        y_combined = pd.concat(all_y, ignore_index=True)
        logger.info(f"Training on {len(X_combined)} rows from {len(all_X)} stocks...")

        model = StockMLModel()
        metrics = model.train_xy(X_combined, y_combined)
        model.save(MODEL_PATH)
        logger.info(f"Model saved — Accuracy: {metrics['accuracy']:.1%}  F1: {metrics['f1']:.3f}")
        return model

    except Exception as e:
        logger.error(f"Model training failed: {e}")
        return None


async def ensure_model_trained():
    """Async wrapper — runs training in thread pool if model is missing or stale."""
    if not _is_stale():
        logger.info("ML model is up-to-date, skipping training")
        return
    logger.info("Training ML model on Nifty50 data (background)...")
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _train_sync)


def get_ml_prediction(symbol: str, closes: list, volumes: list) -> Optional[float]:
    """
    Return a GradientBoosting P(bullish) score (0–100) for the last bar.
    Falls back to None if model not trained.
    """
    try:
        from ml.model import StockMLModel
        import pandas as pd, numpy as np

        if not os.path.exists(MODEL_PATH):
            return None

        model = StockMLModel.load(MODEL_PATH)
        if not model.is_trained:
            return None

        n = len(closes)
        if n < 60:
            return None

        df = pd.DataFrame({
            "close":  closes,
            "open":   closes,   # approximate — we only have close & volume
            "high":   [c * 1.005 for c in closes],
            "low":    [c * 0.995 for c in closes],
            "volume": volumes,
        })
        scores = model.score(df)
        if scores.empty:
            return None
        return float(scores.iloc[-1])

    except Exception as e:
        logger.debug(f"ML prediction failed for {symbol}: {e}")
        return None
