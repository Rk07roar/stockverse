"""
StockVest — ml/model.py
ML model training, validation, and inference pipeline.
Uses scikit-learn RandomForest + gradient boosting for stock scoring.
"""

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import train_test_split, cross_val_score
from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score
from sklearn.pipeline import Pipeline
from typing import Optional
import joblib
import os
import logging

logger = logging.getLogger(__name__)

MODEL_PATH = os.path.join(os.path.dirname(__file__), 'saved_model.pkl')

# ─── Feature Engineering ─────────────────────────────────────
def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute technical and fundamental features from raw OHLCV + fundamental data.
    All features are designed to be forward-looking predictors.
    """
    feat = pd.DataFrame(index=df.index)

    # --- Price momentum features ---
    feat['ret_1d']  = df['close'].pct_change(1)
    feat['ret_5d']  = df['close'].pct_change(5)
    feat['ret_10d'] = df['close'].pct_change(10)
    feat['ret_20d'] = df['close'].pct_change(20)
    feat['ret_60d'] = df['close'].pct_change(60)
    feat['ret_90d'] = df['close'].pct_change(90)

    # --- Moving averages ---
    feat['ma_5']    = df['close'].rolling(5).mean()
    feat['ma_20']   = df['close'].rolling(20).mean()
    feat['ma_50']   = df['close'].rolling(50).mean()
    feat['ma_200']  = df['close'].rolling(200).mean()
    feat['above_50dma']  = (df['close'] > feat['ma_50']).astype(int)
    feat['above_200dma'] = (df['close'] > feat['ma_200']).astype(int)
    feat['price_vs_50']  = (df['close'] - feat['ma_50']) / feat['ma_50']
    feat['price_vs_200'] = (df['close'] - feat['ma_200']) / feat['ma_200']
    feat['golden_cross'] = (feat['ma_50'] > feat['ma_200']).astype(int)

    # --- RSI (14-day) ---
    delta = df['close'].diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    rs    = gain / (loss + 1e-10)
    feat['rsi_14'] = 100 - (100 / (1 + rs))
    feat['rsi_oversold']   = (feat['rsi_14'] < 30).astype(int)
    feat['rsi_overbought'] = (feat['rsi_14'] > 70).astype(int)

    # --- MACD ---
    ema_12 = df['close'].ewm(span=12, adjust=False).mean()
    ema_26 = df['close'].ewm(span=26, adjust=False).mean()
    feat['macd']        = ema_12 - ema_26
    feat['macd_signal'] = feat['macd'].ewm(span=9, adjust=False).mean()
    feat['macd_hist']   = feat['macd'] - feat['macd_signal']
    feat['macd_bullish']= (feat['macd'] > feat['macd_signal']).astype(int)

    # --- Bollinger Bands ---
    bb_mid  = df['close'].rolling(20).mean()
    bb_std  = df['close'].rolling(20).std()
    feat['bb_upper']  = bb_mid + 2 * bb_std
    feat['bb_lower']  = bb_mid - 2 * bb_std
    feat['bb_pct']    = (df['close'] - feat['bb_lower']) / (feat['bb_upper'] - feat['bb_lower'] + 1e-10)
    feat['bb_squeeze']= (bb_std / bb_mid < 0.03).astype(int)   # matches ml/features.py

    # --- Volume features ---
    feat['vol_ratio_5d']  = df['volume'] / (df['volume'].rolling(5).mean() + 1)
    feat['vol_ratio_20d'] = df['volume'] / (df['volume'].rolling(20).mean() + 1)
    feat['vol_surge']     = (feat['vol_ratio_5d'] > 2.0).astype(int)

    # --- Volatility ---
    feat['atr_14']    = df['high'].rolling(14).max() - df['low'].rolling(14).min()
    feat['volatility']= df['close'].pct_change().rolling(20).std() * np.sqrt(252)

    # --- 52-week range ---
    feat['high_52w'] = df['high'].rolling(252).max()
    feat['low_52w']  = df['low'].rolling(252).min()
    feat['pct_from_52w_high'] = (df['close'] - feat['high_52w']) / feat['high_52w']
    feat['pct_from_52w_low']  = (df['close'] - feat['low_52w'])  / feat['low_52w']
    feat['near_52w_high'] = (feat['pct_from_52w_high'] > -0.05).astype(int)

    # --- Fundamental features (if available) ---
    if 'pe' in df.columns:
        feat['pe_ratio']      = df['pe'].fillna(df['pe'].median())
        feat['pb_ratio']      = df.get('pb', pd.Series(np.nan, index=df.index)).fillna(3)
        feat['roe']           = df.get('roe', pd.Series(np.nan, index=df.index)).fillna(15)
        feat['debt_equity']   = df.get('de', pd.Series(np.nan, index=df.index)).fillna(1)
        feat['rev_growth']    = df.get('rev_growth', pd.Series(np.nan, index=df.index)).fillna(0)
        feat['eps_growth']    = df.get('eps_growth', pd.Series(np.nan, index=df.index)).fillna(0)
        feat['promoter_hold'] = df.get('promoter', pd.Series(np.nan, index=df.index)).fillna(50)

    return feat.dropna()


# ─── Target: 1 if stock beats Nifty by >5% in next 30 days ──
def build_target(df: pd.DataFrame, forward_days: int = 30, alpha_threshold: float = 0.05) -> pd.Series:
    future_ret  = df['close'].shift(-forward_days) / df['close'] - 1
    nifty_ret   = df.get('nifty_ret', pd.Series(0.01, index=df.index))  # benchmark
    excess      = future_ret - nifty_ret
    return (excess > alpha_threshold).astype(int)


# ─── Model class ─────────────────────────────────────────────
class StockMLModel:

    def __init__(self):
        self.pipeline = Pipeline([
            ('scaler', StandardScaler()),
            ('model',  GradientBoostingClassifier(
                n_estimators=200,
                learning_rate=0.05,
                max_depth=4,
                subsample=0.8,
                min_samples_leaf=20,
                random_state=42,
            )),
        ])
        self.is_trained   = False
        self.feature_cols = []
        self.metrics      = {}

    def train(self, df: pd.DataFrame) -> dict:
        """Train the model on historical OHLCV + fundamental data (single-stock df)."""
        logger.info("Building features...")
        X = build_features(df)
        y = build_target(df)

        # Align indices — works correctly only for single-stock DataFrames
        # (no duplicate dates). For multi-stock data use train_xy() instead.
        common = X.index.intersection(y.index)
        X, y   = X.loc[common], y.loc[common]
        X.replace([np.inf, -np.inf], np.nan, inplace=True)
        X.fillna(X.median(), inplace=True)

        self.feature_cols = X.columns.tolist()
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.2, shuffle=False
        )

        logger.info(f"Training on {len(X_train)} samples, testing on {len(X_test)}...")
        self.pipeline.fit(X_train, y_train)

        y_pred    = self.pipeline.predict(X_test)
        cv_scores = cross_val_score(self.pipeline, X_train, y_train, cv=5, scoring='accuracy')

        self.metrics = {
            'accuracy':   round(accuracy_score(y_test, y_pred), 4),
            'precision':  round(precision_score(y_test, y_pred, zero_division=0), 4),
            'recall':     round(recall_score(y_test, y_pred, zero_division=0), 4),
            'f1':         round(f1_score(y_test, y_pred, zero_division=0), 4),
            'cv_mean':    round(cv_scores.mean(), 4),
            'cv_std':     round(cv_scores.std(), 4),
            'train_size': len(X_train),
            'test_size':  len(X_test),
        }
        self.is_trained = True
        logger.info(f"Model trained — Accuracy: {self.metrics['accuracy']:.1%}, F1: {self.metrics['f1']:.3f}")
        return self.metrics

    def train_xy(self, X: pd.DataFrame, y: pd.Series) -> dict:
        """
        Train directly from pre-aligned feature/target arrays.
        Use this for multi-stock training where per-stock alignment is done
        before concatenation (avoids duplicate-date index collision).
        """
        X = X.copy()
        X.replace([np.inf, -np.inf], np.nan, inplace=True)
        X.fillna(X.median(), inplace=True)

        self.feature_cols = X.columns.tolist()
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.2, shuffle=False
        )

        logger.info(f"Training on {len(X_train)} samples, testing on {len(X_test)}...")
        self.pipeline.fit(X_train, y_train)

        y_pred    = self.pipeline.predict(X_test)
        cv_scores = cross_val_score(self.pipeline, X_train, y_train, cv=5, scoring='accuracy')

        self.metrics = {
            'accuracy':   round(accuracy_score(y_test, y_pred), 4),
            'precision':  round(precision_score(y_test, y_pred, zero_division=0), 4),
            'recall':     round(recall_score(y_test, y_pred, zero_division=0), 4),
            'f1':         round(f1_score(y_test, y_pred, zero_division=0), 4),
            'cv_mean':    round(cv_scores.mean(), 4),
            'cv_std':     round(cv_scores.std(), 4),
            'train_size': len(X_train),
            'test_size':  len(X_test),
        }
        self.is_trained = True
        logger.info(f"Model trained — Accuracy: {self.metrics['accuracy']:.1%}, F1: {self.metrics['f1']:.3f}")
        return self.metrics

    def score(self, df: pd.DataFrame) -> pd.Series:
        """Return ML score (0-100) for each row in df."""
        if not self.is_trained:
            raise RuntimeError("Model not trained. Call .train() first or load a saved model.")

        X = build_features(df)[self.feature_cols]
        X.replace([np.inf, -np.inf], np.nan, inplace=True)
        X.fillna(X.median(), inplace=True)

        proba = self.pipeline.predict_proba(X)[:, 1]   # P(bullish)
        return pd.Series(np.round(proba * 100).astype(int), index=X.index, name='ml_score')

    def feature_importance(self) -> dict:
        """Returns feature importance dict sorted descending."""
        if not self.is_trained:
            return {}
        model = self.pipeline.named_steps['model']
        imp   = model.feature_importances_
        return dict(sorted(zip(self.feature_cols, imp.tolist()), key=lambda x: -x[1]))

    def save(self, path: str = MODEL_PATH):
        joblib.dump({'pipeline': self.pipeline, 'features': self.feature_cols, 'metrics': self.metrics}, path)
        logger.info(f"Model saved to {path}")

    @classmethod
    def load(cls, path: str = MODEL_PATH) -> 'StockMLModel':
        obj   = cls()
        saved = joblib.load(path)
        obj.pipeline      = saved['pipeline']
        obj.feature_cols  = saved['features']
        obj.metrics       = saved['metrics']
        obj.is_trained    = True
        logger.info(f"Model loaded from {path} — Accuracy: {obj.metrics.get('accuracy', 0):.1%}")
        return obj


# ─── Singleton ───────────────────────────────────────────────
_model_instance: Optional[StockMLModel] = None

def get_model() -> StockMLModel:
    global _model_instance
    if _model_instance is None:
        if os.path.exists(MODEL_PATH):
            _model_instance = StockMLModel.load()
        else:
            _model_instance = StockMLModel()
            logger.warning("No saved model found. Run training first.")
    return _model_instance
