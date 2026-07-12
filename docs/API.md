# StockVest API Documentation

Base URL: `http://localhost:8000`  
Interactive docs: `http://localhost:8000/docs`

## Authentication
All protected endpoints require `Authorization: Bearer <token>` header.  
Get a token via `POST /api/auth/login`.

---

## Stocks

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/stocks/all` | All NSE+BSE stocks (paginated) |
| GET | `/api/stocks/quote/{symbol}` | Real-time quote |
| GET | `/api/stocks/history/{symbol}` | Price history (1d/1w/1m/3m/6m/1y/5y) |
| GET | `/api/stocks/search?q=` | Search by name/symbol |
| GET | `/api/stocks/gainers` | Top gainers today |
| GET | `/api/stocks/losers` | Top losers today |
| GET | `/api/stocks/indices` | Nifty/Sensex/Bank index values |

### GET /api/stocks/all — Query Params
| Param | Type | Default | Description |
|-------|------|---------|-------------|
| page | int | 1 | Page number |
| page_size | int | 100 | Results per page (max 1000) |
| search | str | "" | Search query |
| exchange | str | "" | nse / bse / both |
| sector | str | "" | IT / Bank / Pharma etc |
| sort | str | name | name / chg_desc / ml_desc / price_desc |

---

## Screener

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/screener/run` | Run screener with criteria |
| GET | `/api/screener/presets` | Saved preset screens |

---

## ML Engine

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/ml/scores` | ML scores for all stocks |
| GET | `/api/ml/top-picks` | High conviction picks |
| GET | `/api/ml/sector-momentum` | Sector momentum scores |
| GET | `/api/ml/model-metrics` | Model accuracy, Sharpe, alpha |

---

## Portfolio

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/portfolio/analyse` | XIRR, metrics, health score |
| GET | `/api/portfolio/holdings/{user_id}` | User holdings |

---

## Institutional

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/institutional/fii-dii` | FII/DII flows |
| GET | `/api/institutional/block-deals` | Block and bulk deals |
| GET | `/api/institutional/promoter-changes` | Promoter holding changes |

---

## Backtest

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/backtest/run` | Run strategy backtest |

### POST /api/backtest/run — Body
```json
{
  "strategy": "momentum",
  "universe": "nifty50",
  "start_year": 2014,
  "end_year": 2025,
  "initial_capital": 1000000,
  "rebalance": "monthly",
  "txn_cost": 0.002
}
```
