"""
StockVest — backend/tools/refresh_bse_names.py

One-off fix for missing company names in the "All Stocks" table (rows showing
"—" instead of a name, usually for pure BSE scrip codes like 509930, 511034).

Why this happens: data/fetcher.py's background _fetch_symbol_names() task is
supposed to pull BSE's full ~5,000-scrip list and save it to
data/bse_name_map.json on every startup. If the backend process died or never
started cleanly (e.g. it kept getting blocked before you fixed the uvicorn
Application Control issue), that background task never finished, so the map
is stuck at a partial snapshot.

This script calls the exact same fetch functions your backend already has
(fetch_nse_equity_list / fetch_bse_equity_list in data/nse_fetcher.py) and
merges any newly-resolved names into bse_name_map.json — WITHOUT deleting
any existing entries, so it's safe to run any time.

Run:
    cd backend
    python tools/refresh_bse_names.py

Then restart the backend (or just refresh the All Stocks page once the
backend picks up the updated bse_name_map.json on its next load).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.nse_fetcher import fetch_nse_equity_list, fetch_bse_equity_list

MAP_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "bse_name_map.json")


def main():
    # Load existing map so we only ever ADD, never remove/shrink.
    existing = {}
    if os.path.exists(MAP_PATH):
        with open(MAP_PATH, "r") as f:
            existing = json.load(f)
    print(f"Existing map: {len(existing)} BSE codes with names")

    print("Fetching NSE equity list (for cross-link dedup)...")
    nse_list = fetch_nse_equity_list()
    nse_symbols = {s["nse-code"] for s in nse_list if s.get("nse-code")}
    print(f"  {len(nse_list)} NSE symbols")

    print("Fetching BSE equity list (this is the slow one, ~10-20s)...")
    bse_list = fetch_bse_equity_list()
    print(f"  {len(bse_list)} BSE scrips returned")

    if not bse_list:
        print("\nBSE fetch returned 0 results. This usually means bseindia.com")
        print("blocked the request (rate limit / IP block) or is temporarily")
        print("down. Wait a few minutes and re-run this script.")
        return

    added = 0
    updated = 0
    for s in bse_list:
        code = s.get("bse-code", "").strip()
        name = s.get("name", "").strip()
        if not code or not name or name.isdigit():
            continue
        if code not in existing:
            added += 1
        elif existing[code] != name:
            updated += 1
        existing[code] = name

    tmp = MAP_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(existing, f)
    os.replace(tmp, MAP_PATH)

    print(f"\nDone. {added} new names added, {updated} existing names refreshed.")
    print(f"Total names in map now: {len(existing)}")
    print(f"Saved to: {MAP_PATH}")
    print("\nRestart the backend (or wait for its next load_symbols() call) to see the update.")


if __name__ == "__main__":
    main()
