"""
StockVest — ml/backtest_engine.py
Vectorised backtesting engine for factor strategies.
"""
import numpy as np
import pandas as pd
from typing import List, Optional

def run(
    prices: pd.DataFrame,          # columns = symbols, index = dates
    signals: pd.DataFrame,          # same shape, signal = 1/0/-1
    initial_capital: float = 1_000_000,
    txn_cost: float = 0.002,
    rebalance_freq: str = "M",      # pandas offset alias
) -> dict:
    """
    Run a vectorised backtest.
    Returns equity curve, metrics, and trade log.
    """
    rets      = prices.pct_change().shift(-1)
    rebalance = signals.resample(rebalance_freq).last().reindex(signals.index, method='ffill')

    weights   = rebalance.div(rebalance.abs().sum(axis=1), axis=0).fillna(0)
    port_rets = (weights * rets).sum(axis=1)

    # Transaction costs on turnover
    turnover      = weights.diff().abs().sum(axis=1)
    port_rets    -= turnover * txn_cost

    equity = (1 + port_rets).cumprod() * initial_capital

    # Benchmark (equal weight buy-and-hold)
    bench_rets = rets.mean(axis=1)
    benchmark  = (1 + bench_rets).cumprod() * initial_capital

    ann = 252
    cagr   = (equity.iloc[-1]/initial_capital)**(ann/len(equity)) - 1
    vol    = port_rets.std() * ann**0.5
    sharpe = (cagr - 0.065) / vol if vol else 0
    mdd    = ((equity - equity.expanding().max()) / equity.expanding().max()).min()
    wins   = (port_rets > 0).sum() / len(port_rets)

    return {
        "equity":    equity.tolist(),
        "benchmark": benchmark.tolist(),
        "dates":     [str(d.date()) for d in equity.index],
        "metrics": {
            "cagr":        round(cagr*100, 2),
            "sharpe":      round(sharpe, 2),
            "max_drawdown":round(mdd*100, 2),
            "win_rate":    round(wins*100, 1),
            "final_value": round(equity.iloc[-1]),
        }
    }
