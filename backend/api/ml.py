"""StockVest — api/ml.py  (real technical scoring)"""
from fastapi import APIRouter, Query, HTTPException
from data.fetcher import DataFetcher
from ml.scoring import compute_score, score_batch

router = APIRouter()

@router.get("/bulk", summary="Real ML scores for multiple symbols (comma-separated)")
async def get_ml_bulk(symbols: str, limit: int = Query(50, le=100)):
    """
    Score up to 100 symbols concurrently. Results are cached 4 h so repeated calls are instant.
    Returns {symbol: {score, signal, rsi, macd, ma50, ma200, golden_cross, bb_pct, ret5d, ret20d}}
    """
    sym_list = [s.strip().upper() for s in symbols.split(",") if s.strip()][:limit]
    if not sym_list:
        return {"results": {}}

    # score_batch runs them concurrently, each result cached 4 h
    scores = await score_batch(sym_list)
    return {
        "results": {
            sym: {
                "score":        r.get("score", 50),
                "signal":       r.get("signal", "HOLD"),
                "rsi":          r.get("rsi"),
                "macd":         r.get("macd"),
                "ma50":         r.get("ma50"),
                "ma200":        r.get("ma200"),
                "golden_cross": r.get("golden_cross"),
                "bb_pct":       r.get("bb_pct"),
                "ret5d":        r.get("ret5d"),
                "ret20d":       r.get("ret20d"),
                "real_data":    r.get("real_data", False),
            }
            for sym, r in scores.items()
        }
    }


@router.get("/scores", summary="ML scores for top stocks (bulk list)")
async def get_ml_scores(limit: int = Query(100), sort: str = Query("ml_desc")):
    data = await DataFetcher.get_all_stocks(sort=sort)
    return {"total": len(data), "stocks": data[:limit]}


@router.get("/{symbol}", summary="Real technical score for a single stock")
async def get_ml_score(symbol: str):
    stock = await DataFetcher.get_quote(symbol.upper())
    if not stock:
        raise HTTPException(status_code=404, detail=f"Symbol '{symbol}' not found")

    result = await compute_score(symbol.upper())
    return {
        "symbol":      symbol.upper(),
        "name":        stock.get("name", ""),
        "price":       stock.get("price", 0),
        "change_pct":  stock.get("change_pct", 0),
        "ml_score":    result["score"],
        "signal":      result.get("signal", "HOLD"),
        "rsi":         result.get("rsi", None),
        "macd":        result.get("macd", None),
        "ma50":        result.get("ma50", None),
        "ma200":       result.get("ma200", None),
        "golden_cross":result.get("golden_cross", None),
        "bb_pct":      result.get("bb_pct", None),
        "ret5d":       result.get("ret5d", None),
        "ret20d":      result.get("ret20d", None),
        "components":  result.get("components", {}),
        "real_data":   result.get("real_data", False),
        "recommendation": (
            "Strong accumulation signal — multiple indicators aligned bullish"
            if result["score"] >= 75 else
            "Monitor closely — moderate signal, wait for confirmation"
            if result["score"] >= 50 else
            "Exercise caution — bearish technical setup"
        ),
    }
