"""
StockVest — ml/features.py
Feature engineering pipeline: technical + fundamental indicators.
"""
import numpy as np
import pandas as pd

def compute_rsi(series, period=14):
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    return 100 - 100/(1 + gain/(loss+1e-10))

def compute_macd(series, fast=12, slow=26, signal=9):
    ema_f = series.ewm(span=fast, adjust=False).mean()
    ema_s = series.ewm(span=slow, adjust=False).mean()
    macd  = ema_f - ema_s
    sig   = macd.ewm(span=signal, adjust=False).mean()
    return macd, sig, macd - sig

def compute_bollinger(series, period=20, std=2):
    mid = series.rolling(period).mean()
    sd  = series.rolling(period).std()
    return mid + std*sd, mid, mid - std*sd

def build_features(df: pd.DataFrame) -> pd.DataFrame:
    f = pd.DataFrame(index=df.index)
    c = df['close']

    # Momentum
    for d in [1,5,10,20,60,90]:
        f[f'ret_{d}d'] = c.pct_change(d)

    # Moving averages
    for w in [5,20,50,200]:
        f[f'ma_{w}'] = c.rolling(w).mean()
    f['above_50dma']  = (c > f['ma_50']).astype(int)
    f['above_200dma'] = (c > f['ma_200']).astype(int)
    f['price_vs_50']  = (c - f['ma_50'])  / f['ma_50']
    f['price_vs_200'] = (c - f['ma_200']) / f['ma_200']
    f['golden_cross'] = (f['ma_50'] > f['ma_200']).astype(int)

    # RSI
    f['rsi_14']      = compute_rsi(c)
    f['rsi_oversold']   = (f['rsi_14'] < 30).astype(int)
    f['rsi_overbought'] = (f['rsi_14'] > 70).astype(int)

    # MACD
    f['macd'], f['macd_sig'], f['macd_hist'] = compute_macd(c)
    f['macd_bullish'] = (f['macd'] > f['macd_sig']).astype(int)

    # Bollinger Bands
    f['bb_upper'], f['bb_mid'], f['bb_lower'] = compute_bollinger(c)
    f['bb_pct']    = (c - f['bb_lower']) / (f['bb_upper'] - f['bb_lower'] + 1e-10)
    f['bb_squeeze']= ((f['bb_upper'] - f['bb_lower']) / f['bb_mid'] < 0.03).astype(int)

    # Volume
    f['vol_ratio_5d']  = df['volume'] / (df['volume'].rolling(5).mean()  + 1)
    f['vol_ratio_20d'] = df['volume'] / (df['volume'].rolling(20).mean() + 1)
    f['vol_surge']     = (f['vol_ratio_5d'] > 2).astype(int)

    # Volatility
    f['volatility_20'] = c.pct_change().rolling(20).std() * np.sqrt(252)
    f['atr_14']        = (df['high'].rolling(14).max() - df['low'].rolling(14).min())

    # 52-week range
    f['high_52w'] = df['high'].rolling(252).max()
    f['low_52w']  = df['low'].rolling(252).min()
    f['pct_from_52w_high'] = (c - f['high_52w']) / f['high_52w']
    f['pct_from_52w_low']  = (c - f['low_52w'])  / f['low_52w']
    f['near_52w_high'] = (f['pct_from_52w_high'] > -0.05).astype(int)

    # Fundamentals (if present)
    for col in ['pe','pb','roe','de','rev_growth','eps_growth','promoter']:
        if col in df.columns:
            f[col] = df[col].fillna(df[col].median())

    return f.replace([np.inf,-np.inf], np.nan).dropna()
