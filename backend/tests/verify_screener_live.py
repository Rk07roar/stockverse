"""
StockVest — backend/tests/verify_screener_live.py

Cross-verifies that the /api/screener endpoint's FILTER LOGIC is actually
enforcing what it claims to enforce, using the live running server and
real (or cached) fundamentals/technicals data.

This does NOT check whether the underlying numbers (PE, ROE, RSI...) are
"correct" against NSE/Screener.in — that's a data-accuracy check, done
separately by spot-checking a few symbols by hand. This script checks a
different thing: given the numbers the API itself returns for a stock,
does that stock actually satisfy the filter it passed?

How to run:
    1. Start the backend:  python -m uvicorn main:app --port 8000
    2. In another terminal:  python tests/verify_screener_live.py
       (optionally: python tests/verify_screener_live.py --base http://localhost:8000)

Exit code is 0 if all checks pass, 1 if any invariant is violated.
"""
import sys
import argparse
import httpx


def get(base, params):
    r = httpx.get(f"{base}/api/screener/", params=params, timeout=60)
    r.raise_for_status()
    return r.json()


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f"  — {detail}" if detail and not condition else ""))
    return condition


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    failures = 0

    # ── 1. Fundamental bound checks: every returned row must actually
    #      satisfy the min/max it was filtered by. This is the core
    #      "is the filter logic working" check. ──────────────────────
    data = get(base, {"min_roe": 15, "max_de": 1, "enrich": "true", "limit": 100})
    stocks = data["stocks"]
    print(f"\n--- min_roe=15, max_de=1 → {len(stocks)} stocks ---")
    bad_roe = [s["sym"] for s in stocks if s.get("roe") is not None and s["roe"] < 15]
    bad_de  = [s["sym"] for s in stocks if s.get("debt_equity") is not None and s["debt_equity"] > 1]
    failures += not check("all returned stocks have roe >= 15", not bad_roe, f"violators: {bad_roe}")
    failures += not check("all returned stocks have debt_equity <= 1", not bad_de, f"violators: {bad_de}")

    # ── 2. RSI bound check ───────────────────────────────────────────
    data = get(base, {"max_rsi": 30, "technicals": "true", "limit": 100})
    stocks = data["stocks"]
    print(f"\n--- max_rsi=30 (oversold) → {len(stocks)} stocks ---")
    bad_rsi = [s["sym"] for s in stocks if s.get("rsi") is not None and s["rsi"] > 30]
    failures += not check("all returned stocks have rsi <= 30", not bad_rsi, f"violators: {bad_rsi}")
    missing_rsi = [s["sym"] for s in stocks if s.get("rsi") is None]
    failures += not check(
        "no stock with rsi=None slipped through a strict RSI filter",
        not missing_rsi, f"slipped through: {missing_rsi}",
    )

    # ── 3. Boolean flag filters ──────────────────────────────────────
    data = get(base, {"above_200dma": "true", "technicals": "true", "limit": 100})
    stocks = data["stocks"]
    print(f"\n--- above_200dma=true → {len(stocks)} stocks ---")
    bad_flag = [s["sym"] for s in stocks if s.get("above_200dma") is not True]
    failures += not check("every returned stock has above_200dma == True", not bad_flag, f"violators: {bad_flag}")

    # ── 4. Signal filter ──────────────────────────────────────────────
    data = get(base, {"signal": "BUY", "technicals": "true", "limit": 100})
    stocks = data["stocks"]
    print(f"\n--- signal=BUY → {len(stocks)} stocks ---")
    bad_sig = [s["sym"] for s in stocks if "BUY" not in (s.get("signal") or "")]
    failures += not check("every returned stock's signal contains 'BUY'", not bad_sig, f"violators: {bad_sig}")

    # ── 5. Monotonicity: adding a filter should never INCREASE the result count ──
    unfiltered = get(base, {"limit": 200})
    filtered   = get(base, {"min_roe": 15, "enrich": "true", "limit": 200})
    print(f"\n--- monotonicity: unfiltered={unfiltered['total']} vs min_roe=15 filtered={filtered['total']} ---")
    failures += not check(
        "adding min_roe filter does not increase result count",
        filtered["total"] <= unfiltered["total"],
    )

    # ── 6. THE BIG ONE: impossible filter must return zero results.
    #      If this returns anything, it means stocks with missing/failed
    #      fundamental data are bypassing the filter entirely instead of
    #      being excluded — check api/screener.py line ~538
    #      (`if enrich and fund:`) which is falsy-and-skips-filters when
    #      a stock's fundamentals fetch returned an empty dict. ─────────
    data = get(base, {"min_roe": 99999, "enrich": "true", "limit": 200})
    print(f"\n--- impossible filter (min_roe=99999) → {data['total']} stocks (should be 0) ---")
    failures += not check(
        "impossible ROE threshold returns zero stocks (no bypass via empty fundamentals)",
        data["total"] == 0,
        f"got {data['total']} — these stocks bypassed the filter: {[s['sym'] for s in data['stocks']]}",
    )

    # ── 7. Sort order correctness ─────────────────────────────────────
    data = get(base, {"sort_by": "roe", "sort_dir": "desc", "enrich": "true", "limit": 50})
    vals = [s["roe"] for s in data["stocks"] if s.get("roe") is not None]
    is_sorted_desc = all(vals[i] >= vals[i+1] for i in range(len(vals)-1))
    print(f"\n--- sort_by=roe desc → checking {len(vals)} non-null values ---")
    failures += not check("results are sorted by roe descending", is_sorted_desc)

    # ── Summary ────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    if failures == 0:
        print("ALL CHECKS PASSED — filter logic behaves as declared.")
    else:
        print(f"{failures} CHECK(S) FAILED — filter logic has a bug. See FAIL lines above.")
    print("=" * 60)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
