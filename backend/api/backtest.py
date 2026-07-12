"""
StockVest — api/backtest.py
Real strategy backtesting using yfinance historical data.

Strategies:
  ml        — Buy when technical score ≥ 70, sell when ≤ 35
  momentum  — Buy on golden cross + RSI < 65, sell on death cross
  sma_cross — Buy on 50d SMA > 200d SMA, sell on reverse
  buy_hold  — Benchmark buy-and-hold

Returns real metrics: CAGR, Sharpe, max drawdown, win rate, equity curve.
"""
import asyncio
import math
import logging
from typing import Optional
from fastapi import APIRouter, Query, HTTPException

logger = logging.getLogger(__name__)
router = APIRouter()


def _ema(closes, span):
    if not closes:
        return 0.0
    k   = 2 / (span + 1)
    val = closes[0]
    for c in closes[1:]:
        val = c * k + val * (1 - k)
    return val

def _sma_series(closes, period):
    result = []
    for i in range(len(closes)):
        if i < period - 1:
            result.append(None)
        else:
            result.append(sum(closes[i-period+1:i+1]) / period)
    return result

def _rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in deltas]
    losses = [abs(min(d, 0)) for d in deltas]
    avg_g  = sum(gains[:period]) / period
    avg_l  = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period-1) + gains[i]) / period
        avg_l = (avg_l * (period-1) + losses[i]) / period
    return 100 - 100 / (1 + avg_g / (avg_l + 1e-9))

def _compute_technicals_series(closes):
    """
    Multi-factor AI score (0–100) per bar.
    Uses RSI, MACD, price momentum, and 20d/50d trend — all available
    within 50 bars so works on any time period including 1Y.
    """
    n    = len(closes)
    ma20 = _sma_series(closes, 20)
    ma50 = _sma_series(closes, 50)
    signals = []
    for i in range(n):
        if i < 20:
            signals.append(0)
            continue

        window  = closes[max(0, i - 29):i + 1]
        rsi_val = _rsi(window, 14)

        ema12 = _ema(closes[max(0, i - 24):i + 1], 12)
        ema26 = _ema(closes[max(0, i - 35):i + 1], 26)
        macd  = ema12 - ema26

        ret20 = (closes[i] / closes[i - 20] - 1) * 100 if i >= 20 else 0
        ret5  = (closes[i] / closes[i -  5] - 1) * 100 if i >=  5 else 0

        # ── RSI: oversold = high score, overbought = low score ──
        if rsi_val < 30:
            s_rsi = 95
        elif rsi_val < 40:
            s_rsi = 80
        elif rsi_val < 50:
            s_rsi = 65
        elif rsi_val < 60:
            s_rsi = 50
        elif rsi_val < 70:
            s_rsi = 35
        else:
            s_rsi = 15

        # ── MACD: direction + distance from zero ────────────────
        price_scale = closes[i] if closes[i] > 0 else 1
        macd_pct    = macd / price_scale * 100
        s_macd      = max(15, min(90, 55 + macd_pct * 30))

        # ── Short-term momentum ──────────────────────────────────
        s_mom = max(10, min(95, 55 + (ret20 * 0.5 + ret5 * 0.5) * 2.5))

        # ── Trend: price vs 20d/50d MA (works without 200d) ─────
        above20 = ma20[i] and closes[i] > ma20[i]
        above50 = ma50[i] and closes[i] > ma50[i]
        ma_bull = ma20[i] and ma50[i] and ma20[i] > ma50[i]  # short-term uptrend
        if above20 and above50 and ma_bull:
            s_ma = 85  # price above both + 20d > 50d (bullish)
        elif above20 and above50:
            s_ma = 70  # price above both
        elif above20:
            s_ma = 55  # price above 20d only
        elif above50:
            s_ma = 40  # price above 50d only
        else:
            s_ma = 20  # below both MAs

        score = s_rsi * 0.30 + s_macd * 0.25 + s_mom * 0.20 + s_ma * 0.25
        signals.append(round(score, 1))
    return signals


def _run_backtest_logic(closes, dates, strategy, initial_capital=1_000_000):
    n          = len(closes)
    capital    = float(initial_capital)
    position   = 0      # shares held
    in_market  = False
    buy_price  = 0.0

    equity_curve = []
    trade_returns = []

    ma20  = _sma_series(closes, 20)
    ma50  = _sma_series(closes, 50)
    ma200 = _sma_series(closes, 200)

    if strategy == "ml":
        signals = _compute_technicals_series(closes)
        BUY_THR, SELL_THR = 62, 40   # score 0-100; buy when confident, sell on weakness
    elif strategy == "momentum":
        signals = [0.0] * n  # RSI momentum — handled directly in loop
    elif strategy == "sma_cross":
        signals = [0.0] * n
    else:  # buy_hold
        signals = [100.0] * n
        BUY_THR, SELL_THR = 50, -1  # never sell

    txn_cost = 0.002  # 0.2% per trade (brokerage + STT approx)

    for i in range(n):
        price = closes[i]

        # Buy/sell logic per strategy
        if strategy == "ml":
            sig = signals[i]
            if not in_market and sig >= BUY_THR:
                position   = (capital * (1 - txn_cost)) / price
                buy_price  = price
                capital    = 0
                in_market  = True
            elif in_market and sig <= SELL_THR:
                proceeds   = position * price * (1 - txn_cost)
                ret        = (price / buy_price - 1) * 100
                trade_returns.append(ret)
                capital    = proceeds
                position   = 0
                in_market  = False

        elif strategy == "momentum":
            # Pure RSI Mean Reversion — buy oversold, sell overbought.
            # No trend filter (the MA filter caused zero trades on declining stocks
            # because RSI goes below 35 exactly when price breaks below MA).
            # Needs only 15 bars of warm-up, fires on any stock/period.
            if i < 15:
                cur_val = capital + position * price
                equity_curve.append(round(cur_val, 2))
                continue
            rsi_val = _rsi(closes[max(0, i - 29):i + 1], 14)
            if not in_market and rsi_val < 33:          # deeply oversold → buy
                position  = (capital * (1 - txn_cost)) / price
                buy_price = price
                capital   = 0
                in_market = True
            elif in_market and rsi_val > 65:            # overbought → take profit
                proceeds  = position * price * (1 - txn_cost)
                trade_returns.append((price / buy_price - 1) * 100)
                capital   = proceeds
                position  = 0
                in_market = False

        elif strategy == "sma_cross":
            # 20d/50d cross — works on 1Y data (needs only 50 bars)
            gc = ma20[i] and ma50[i] and ma20[i] > ma50[i]
            if not in_market and gc and i >= 50:
                position   = (capital * (1 - txn_cost)) / price
                buy_price  = price
                capital    = 0
                in_market  = True
            elif in_market and not gc:
                proceeds   = position * price * (1 - txn_cost)
                trade_returns.append((price / buy_price - 1) * 100)
                capital    = proceeds
                position   = 0
                in_market  = False

        else:  # buy_hold: buy on day 1
            if i == 0:
                position   = (capital * (1 - txn_cost)) / price
                buy_price  = price
                capital    = 0
                in_market  = True

        cur_val = capital + position * price
        equity_curve.append(round(cur_val, 2))

    # Liquidate at end
    if in_market and position > 0:
        capital = position * closes[-1] * (1 - txn_cost)
        trade_returns.append((closes[-1] / buy_price - 1) * 100)

    final_val = capital + position * closes[-1]

    # ── Metrics ──────────────────────────────────────────────
    years = len(closes) / 252
    if years > 0:
        cagr = ((final_val / initial_capital) ** (1 / years) - 1) * 100
    else:
        cagr = 0

    # Daily returns for Sharpe
    daily_ret = []
    for i in range(1, len(equity_curve)):
        r = (equity_curve[i] / equity_curve[i-1] - 1) if equity_curve[i-1] > 0 else 0
        daily_ret.append(r)

    if daily_ret:
        avg_r = sum(daily_ret) / len(daily_ret)
        std_r = (sum((r - avg_r)**2 for r in daily_ret) / len(daily_ret)) ** 0.5
        # Guard: flat equity curve → std_r ≈ 0 → Sharpe is undefined, treat as 0
        sharpe = (avg_r * 252 - 0.065) / (std_r * (252 ** 0.5)) if std_r > 1e-7 else 0.0
    else:
        sharpe = 0

    # Max drawdown
    peak = equity_curve[0] if equity_curve else initial_capital
    mdd  = 0.0
    for v in equity_curve:
        if v > peak:
            peak = v
        dd = (v - peak) / peak
        if dd < mdd:
            mdd = dd

    win_rate = (
        sum(1 for r in trade_returns if r > 0) / len(trade_returns) * 100
        if trade_returns else 0
    )

    # Benchmark: buy & hold from day 1
    bh_val = initial_capital * (closes[-1] / closes[0]) if closes[0] > 0 else initial_capital
    bh_ret = ((bh_val / initial_capital) ** (1 / years) - 1) * 100 if years > 0 else 0
    benchmark_curve = [
        round(initial_capital * (closes[i] / closes[0]), 2) for i in range(n)
    ]

    # Downsample equity/bench curves to ~200 points max
    step = max(1, n // 200)
    sampled_dates  = dates[::step]
    sampled_equity = equity_curve[::step]
    sampled_bench  = benchmark_curve[::step]

    def _safe(v, decimals=2, fallback=0.0):
        """Return rounded float; replace NaN/Inf with fallback."""
        try:
            r = round(float(v), decimals)
            return fallback if (math.isnan(r) or math.isinf(r)) else r
        except Exception:
            return fallback

    return {
        "metrics": {
            "cagr":         _safe(cagr, 2),
            "bh_cagr":      _safe(bh_ret, 2),
            "sharpe":       _safe(sharpe, 2),
            "max_drawdown": _safe(mdd * 100, 2),
            "win_rate":     _safe(win_rate, 1),
            "total_trades": len(trade_returns),
            "final_value":  _safe(final_val, 2, fallback=float(initial_capital)),
            "initial":      initial_capital,
            "total_return": _safe((final_val / initial_capital - 1) * 100, 2),
        },
        "equity":    [_safe(v, 2) for v in sampled_equity],
        "benchmark": [_safe(v, 2) for v in sampled_bench],
        "dates":     sampled_dates,
    }


@router.get("/run", summary="Run a backtest on a symbol")
async def run_backtest(
    symbol:   str   = Query(..., description="NSE symbol e.g. TCS"),
    period:   str   = Query("3y", description="1y|2y|3y|5y|10y"),
    strategy: str   = Query("ml", description="ml|momentum|sma_cross|buy_hold"),
    capital:  float = Query(1_000_000, description="Starting capital in INR"),
):
    period_map = {"1y": "1y", "2y": "2y", "3y": "3y", "5y": "5y", "10y": "10y"}
    yf_period  = period_map.get(period, "3y")
    sym_upper  = symbol.upper()

    def _sync():
        try:
            import math as _math
            import yfinance as yf
            df = None
            for suffix in [".NS", ".BO"]:
                tk  = yf.Ticker(f"{sym_upper}{suffix}")
                tmp = tk.history(period=yf_period, auto_adjust=True)
                # Drop rows with NaN close prices (trading halts / data gaps)
                tmp = tmp.dropna(subset=["Close"])
                if not tmp.empty and len(tmp) >= 50:
                    df = tmp
                    break
            if df is None or df.empty or len(df) < 50:
                return None, "Insufficient price history for backtesting"
            closes = [float(v) for v in df["Close"].tolist()]
            # Guard: reject any residual NaN/Inf that dropna might have missed
            if any(_math.isnan(c) or _math.isinf(c) for c in closes):
                return None, "Price data contains invalid values — try a different period"
            dates  = [str(d.date()) for d in df.index]
            result = _run_backtest_logic(closes, dates, strategy, initial_capital=capital)
            result["symbol"]   = sym_upper
            result["period"]   = period
            result["strategy"] = strategy
            return result, None
        except Exception as e:
            logger.error(f"Backtest failed for {sym_upper}: {type(e).__name__}: {e}")
            return None, str(e)

    loop = asyncio.get_running_loop()
    result, error = await loop.run_in_executor(None, _sync)

    if error:
        raise HTTPException(400, detail=error)
    return result


@router.get("/strategies", summary="List available strategies")
async def list_strategies():
    return {
        "strategies": [
            {"id": "ml",        "name": "StockVest AI Score",
             "description": "Multi-factor score: RSI + MACD + momentum + trend. Buy ≥62, sell ≤40"},
            {"id": "momentum",  "name": "RSI Mean Reversion",
             "description": "Buy when RSI < 33 (deeply oversold). Sell when RSI > 65 (overbought). Works on any period"},
            {"id": "sma_cross", "name": "SMA Crossover (20/50)",
             "description": "Buy when 20d SMA crosses above 50d. Sell on reversal. Active swing strategy"},
            {"id": "buy_hold",  "name": "Buy & Hold Benchmark",
             "description": "Buy on day 1, hold forever. Buffett's baseline — beat this to win"},
        ]
    }
