"""
StockVest — utils/calculations.py
Financial calculations: XIRR, Sharpe, Sortino, drawdown, ratios.
"""

import numpy as np
import pandas as pd
from datetime import date, datetime
from typing import List, Tuple, Optional
from scipy import optimize


# ─── XIRR ────────────────────────────────────────────────────
def xirr(cashflows: List[Tuple[date, float]], guess: float = 0.1) -> float:
    """
    Compute XIRR (Extended Internal Rate of Return).

    cashflows: list of (date, amount) tuples.
               Investments are negative, returns are positive.
    Returns annualised rate as a decimal (e.g. 0.224 = 22.4%).
    """
    if not cashflows:
        return 0.0

    dates, amounts = zip(*cashflows)
    dates = [d if isinstance(d, date) else d.date() for d in dates]
    t0 = dates[0]
    years = [(d - t0).days / 365.0 for d in dates]

    def npv(rate):
        return sum(a / (1 + rate) ** t for a, t in zip(amounts, years))

    try:
        result = optimize.brentq(npv, -0.999, 100.0, maxiter=1000)
        return round(result, 6)
    except (ValueError, RuntimeError):
        return 0.0


# ─── Returns ─────────────────────────────────────────────────
def cagr(start_value: float, end_value: float, years: float) -> float:
    """Compound Annual Growth Rate."""
    if start_value <= 0 or years <= 0:
        return 0.0
    return round((end_value / start_value) ** (1 / years) - 1, 6)


def annualised_return(returns: pd.Series, periods_per_year: int = 252) -> float:
    """Annualise a series of period returns."""
    n = len(returns)
    if n == 0:
        return 0.0
    total = (1 + returns).prod()
    return round(total ** (periods_per_year / n) - 1, 6)


# ─── Risk Metrics ────────────────────────────────────────────
def sharpe_ratio(returns: pd.Series, risk_free: float = 0.065, periods_per_year: int = 252) -> float:
    """Sharpe Ratio = (Ann. Return - Risk Free) / Ann. Volatility."""
    if len(returns) < 2:
        return 0.0
    ann_ret = annualised_return(returns, periods_per_year)
    ann_vol = returns.std() * np.sqrt(periods_per_year)
    if ann_vol == 0:
        return 0.0
    return round((ann_ret - risk_free) / ann_vol, 4)


def sortino_ratio(returns: pd.Series, risk_free: float = 0.065, periods_per_year: int = 252) -> float:
    """Sortino Ratio — uses downside deviation instead of total vol."""
    if len(returns) < 2:
        return 0.0
    ann_ret = annualised_return(returns, periods_per_year)
    downside = returns[returns < 0]
    downside_std = downside.std() * np.sqrt(periods_per_year)
    if downside_std == 0:
        return 0.0
    return round((ann_ret - risk_free) / downside_std, 4)


def max_drawdown(equity_curve: pd.Series) -> float:
    """Maximum drawdown as a negative decimal."""
    if len(equity_curve) < 2:
        return 0.0
    peak     = equity_curve.expanding().max()
    drawdown = (equity_curve - peak) / peak
    return round(drawdown.min(), 6)


def calmar_ratio(returns: pd.Series, periods_per_year: int = 252) -> float:
    """CAGR / |Max Drawdown|."""
    equity = (1 + returns).cumprod()
    mdd    = abs(max_drawdown(equity))
    ann    = annualised_return(returns, periods_per_year)
    return round(ann / mdd, 4) if mdd != 0 else 0.0


def win_rate(returns: pd.Series) -> float:
    if len(returns) == 0:
        return 0.0
    return round((returns > 0).sum() / len(returns), 4)


def profit_factor(returns: pd.Series) -> float:
    gains  = returns[returns > 0].sum()
    losses = abs(returns[returns < 0].sum())
    return round(gains / losses, 4) if losses != 0 else 0.0


def beta(stock_returns: pd.Series, market_returns: pd.Series) -> float:
    if len(stock_returns) < 2:
        return 1.0
    cov = np.cov(stock_returns, market_returns)[0, 1]
    var = np.var(market_returns)
    return round(cov / var, 4) if var != 0 else 1.0


def alpha(stock_returns: pd.Series, market_returns: pd.Series, risk_free: float = 0.065, periods_per_year: int = 252) -> float:
    b    = beta(stock_returns, market_returns)
    ann  = annualised_return(stock_returns, periods_per_year)
    mret = annualised_return(market_returns, periods_per_year)
    return round(ann - (risk_free + b * (mret - risk_free)), 6)


# ─── Full Portfolio Analytics ─────────────────────────────────
def portfolio_metrics(equity_curve: pd.Series, benchmark: Optional[pd.Series] = None, risk_free: float = 0.065) -> dict:
    """
    Compute full set of portfolio performance metrics.
    equity_curve: series of portfolio NAV (starting at 100).
    benchmark: optional benchmark NAV series.
    """
    returns    = equity_curve.pct_change().dropna()
    ann_return = annualised_return(returns)
    ann_vol    = returns.std() * np.sqrt(252)
    mdd        = max_drawdown(equity_curve)
    years      = len(returns) / 252

    metrics = {
        'total_return':    round(equity_curve.iloc[-1] / equity_curve.iloc[0] - 1, 4),
        'cagr':            round(cagr(equity_curve.iloc[0], equity_curve.iloc[-1], years), 4),
        'annualised_vol':  round(ann_vol, 4),
        'sharpe':          sharpe_ratio(returns, risk_free),
        'sortino':         sortino_ratio(returns, risk_free),
        'calmar':          calmar_ratio(returns),
        'max_drawdown':    round(mdd, 4),
        'win_rate':        win_rate(returns),
        'profit_factor':   profit_factor(returns),
    }

    if benchmark is not None:
        bench_rets = benchmark.pct_change().dropna()
        metrics['beta']  = beta(returns, bench_rets)
        metrics['alpha'] = alpha(returns, bench_rets, risk_free)

    return metrics


# ─── Valuation Ratios ─────────────────────────────────────────
def pe_ratio(price: float, eps: float) -> Optional[float]:
    return round(price / eps, 2) if eps and eps > 0 else None

def pb_ratio(price: float, book_value: float) -> Optional[float]:
    return round(price / book_value, 2) if book_value and book_value > 0 else None

def ev_ebitda(market_cap: float, total_debt: float, cash: float, ebitda: float) -> Optional[float]:
    ev = market_cap + total_debt - cash
    return round(ev / ebitda, 2) if ebitda and ebitda > 0 else None

def roe(net_income: float, shareholders_equity: float) -> Optional[float]:
    return round(net_income / shareholders_equity * 100, 2) if shareholders_equity and shareholders_equity > 0 else None

def roce(ebit: float, capital_employed: float) -> Optional[float]:
    return round(ebit / capital_employed * 100, 2) if capital_employed and capital_employed > 0 else None

def debt_to_equity(total_debt: float, shareholders_equity: float) -> Optional[float]:
    return round(total_debt / shareholders_equity, 2) if shareholders_equity and shareholders_equity > 0 else None

def current_ratio(current_assets: float, current_liabilities: float) -> Optional[float]:
    return round(current_assets / current_liabilities, 2) if current_liabilities and current_liabilities > 0 else None

def dividend_yield(annual_dividend: float, price: float) -> Optional[float]:
    return round(annual_dividend / price * 100, 2) if price and price > 0 else None
