"""
StockVest — backend/tests/test_screener_logic.py

Unit tests for the screener's FILTER LOGIC ITSELF (api/screener.py), independent
of any live market data or network calls. These test the pure functions with
known synthetic inputs so you can trust the math regardless of what NSE/yfinance
returns on a given day.

Run:
    cd backend
    pytest tests/test_screener_logic.py -v

Requires: pip install pytest
"""
import math
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.screener import _ok, _rsi, _sma, _ema, _bollinger_width, _quality_score, _compute_technicals


# ── _ok() — the core filter comparator ─────────────────────────────
class TestOkFilter:
    def test_within_range_passes(self):
        assert _ok(10, 5, 20) is True

    def test_below_min_fails(self):
        assert _ok(3, 5, 20) is False

    def test_above_max_fails(self):
        assert _ok(25, 5, 20) is False

    def test_no_bounds_always_passes(self):
        assert _ok(999, None, None) is True

    def test_only_min_set(self):
        assert _ok(10, 5, None) is True
        assert _ok(2, 5, None) is False

    def test_only_max_set(self):
        assert _ok(10, None, 20) is True
        assert _ok(30, None, 20) is False

    def test_boundary_values_inclusive(self):
        # lo/hi should be inclusive (>=, <=), not exclusive
        assert _ok(5, 5, 20) is True
        assert _ok(20, 5, 20) is True

    def test_none_value_lenient_by_default(self):
        # strict=False (default): missing data should NOT be filtered out
        assert _ok(None, 5, 20) is True

    def test_none_value_strict_fails(self):
        # strict=True: missing data SHOULD be filtered out
        assert _ok(None, 5, 20, strict=True) is False

    def test_none_value_strict_but_no_bounds_still_fails(self):
        # KNOWN GOTCHA: if strict=True is hardcoded (as it is for pe/pb/ps in
        # screen()), a stock with missing data gets excluded even when the
        # user set NO min/max for that field at all. Confirm this is really
        # how _ok behaves so the caller can decide if that's intended.
        assert _ok(None, None, None, strict=True) is False


# ── RSI ──────────────────────────────────────────────────────────────
class TestRSI:
    def test_insufficient_data_returns_none(self):
        assert _rsi([100, 101, 102], period=14) is None

    def test_all_gains_rsi_is_100(self):
        closes = [100 + i for i in range(20)]  # strictly increasing
        assert _rsi(closes) == 100.0

    def test_all_losses_rsi_is_zero(self):
        closes = [100 - i for i in range(20)]  # strictly decreasing
        assert _rsi(closes) == 0.0

    def test_known_rsi_value(self):
        # Classic textbook RSI example (Wilder's original 14-day dataset,
        # simplified SMA version). Verifies the formula, not just direction.
        closes = [
            44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
            45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28,
        ]
        rsi = _rsi(closes, period=14)
        # Expected ~70.5 with simple-average RSI (not Wilder's smoothed version)
        assert 65 <= rsi <= 75, f"RSI {rsi} out of expected sanity range"


# ── SMA / EMA / Bollinger ──────────────────────────────────────────
class TestMovingAverages:
    def test_sma_basic(self):
        assert _sma([1, 2, 3, 4, 5], period=5) == 3.0

    def test_sma_insufficient_data(self):
        assert _sma([1, 2], period=5) is None

    def test_sma_uses_last_n_only(self):
        # SMA(3) of [1,2,3,4,5,6] should be mean of last 3 = (4+5+6)/3 = 5
        assert _sma([1, 2, 3, 4, 5, 6], period=3) == 5.0

    def test_ema_converges_toward_flat_price(self):
        closes = [100] * 30
        ema = _ema(closes, period=20)
        assert abs(ema[-1] - 100) < 0.01

    def test_bollinger_width_zero_for_flat_price(self):
        closes = [100] * 25
        assert _bollinger_width(closes) == 0.0

    def test_bollinger_width_positive_for_volatile_price(self):
        closes = [100, 110, 90, 105, 95] * 5
        w = _bollinger_width(closes)
        assert w is not None and w > 0


# ── Composite quality score ─────────────────────────────────────────
class TestQualityScore:
    def test_score_bounded_0_100(self):
        # Even with extreme inputs, score must stay in [0, 100]
        fund = {"roe": 5.0, "pe": 1, "debt_equity": 10, "profit_margin": -2.0, "revenue_growth": 5.0}
        tech = {"above_200dma": False, "death_cross": True, "rsi": 90, "vol_ratio_20d": 0.1}
        score = _quality_score(fund, tech, ml_score=0)
        assert 0 <= score <= 100

    def test_strong_fundamentals_score_higher_than_weak(self):
        strong_fund = {"roe": 0.30, "pe": 12, "debt_equity": 0.1, "profit_margin": 0.25, "revenue_growth": 0.25}
        weak_fund   = {"roe": 0.02, "pe": 60, "debt_equity": 3.0, "profit_margin": -0.05, "revenue_growth": 0.0}
        strong = _quality_score(strong_fund, {}, ml_score=50)
        weak   = _quality_score(weak_fund, {}, ml_score=50)
        assert strong > weak, f"expected strong={strong} > weak={weak}"

    def test_technicals_move_score_in_expected_direction(self):
        fund = {}
        bullish = {"above_200dma": True, "above_50ema": True, "golden_cross": True, "rsi": 50}
        bearish = {"above_200dma": False, "above_50ema": False, "death_cross": True, "rsi": 80}
        assert _quality_score(fund, bullish, 0) > _quality_score(fund, bearish, 0)

    def test_ml_score_contributes_positively(self):
        base = _quality_score({}, {}, ml_score=0)
        boosted = _quality_score({}, {}, ml_score=100)
        assert boosted > base


# ── _compute_technicals — sanity + flag correctness ─────────────────
class TestComputeTechnicals:
    def test_empty_input_returns_empty_dict(self):
        assert _compute_technicals([], []) == {}

    def test_above_200dma_flag_correct(self):
        # Price trending up, ends well above its own 200-period average
        closes = [100 + i * 0.5 for i in range(250)]
        volumes = [1000] * 250
        tech = _compute_technicals(closes, volumes)
        assert tech["above_200dma"] is True

    def test_below_200dma_flag_correct(self):
        closes = [200 - i * 0.5 for i in range(250)]
        volumes = [1000] * 250
        tech = _compute_technicals(closes, volumes)
        assert tech["above_200dma"] is False

    def test_pct_from_ath_is_never_positive(self):
        # Current price can never be above its own 52w high by definition
        closes = [100 + (i % 10) for i in range(260)]
        volumes = [1000] * 260
        tech = _compute_technicals(closes, volumes)
        assert tech["pct_from_ath"] <= 0

    def test_pct_from_atl_is_never_negative(self):
        closes = [100 + (i % 10) for i in range(260)]
        volumes = [1000] * 260
        tech = _compute_technicals(closes, volumes)
        assert tech["pct_from_atl"] >= 0
