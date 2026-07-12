"""
StockVest — db.py
SQLite database: portfolio holdings, transactions, watchlist.
Uses aiosqlite for async access compatible with FastAPI.
"""
import os
import aiosqlite

DB_PATH = os.path.join(os.path.dirname(__file__), "stockvest.db")

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS holdings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT    NOT NULL DEFAULT 'guest',
    symbol      TEXT    NOT NULL,
    name        TEXT    NOT NULL DEFAULT '',
    exchange    TEXT    NOT NULL DEFAULT 'NSE',
    qty         REAL    NOT NULL,
    avg_price   REAL    NOT NULL,
    buy_date    TEXT    NOT NULL,
    notes       TEXT    DEFAULT ''
);

CREATE TABLE IF NOT EXISTS transactions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT    NOT NULL DEFAULT 'guest',
    symbol      TEXT    NOT NULL,
    name        TEXT    NOT NULL DEFAULT '',
    action      TEXT    NOT NULL CHECK(action IN ('BUY','SELL')),
    qty         REAL    NOT NULL,
    price       REAL    NOT NULL,
    total       REAL    NOT NULL,
    date        TEXT    NOT NULL,
    notes       TEXT    DEFAULT ''
);

CREATE TABLE IF NOT EXISTS watchlist (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT    NOT NULL DEFAULT 'guest',
    symbol      TEXT    NOT NULL,
    name        TEXT    NOT NULL DEFAULT '',
    added_at    TEXT    NOT NULL,
    UNIQUE(user_id, symbol)
);

CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT    NOT NULL DEFAULT 'guest',
    symbol      TEXT    NOT NULL,
    condition   TEXT    NOT NULL CHECK(condition IN ('above','below','ml_above','ml_below','volume_spike')),
    target      REAL    NOT NULL,
    triggered   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    email       TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    hashed_pw   TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (date('now')),
    is_active   INTEGER NOT NULL DEFAULT 1
);
"""

async def init_db():
    """Create tables if they don't exist, and run schema migrations."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(_CREATE_SQL)
        # ── Migrations: add columns introduced after initial schema ──
        for table, col, definition in [
            ("holdings",     "source",            "TEXT DEFAULT 'manual'"),
            ("transactions", "source",            "TEXT DEFAULT 'manual'"),
            # Alerts: notification channels added post-launch
            ("alerts",       "notify_email",      "TEXT DEFAULT ''"),
            ("alerts",       "telegram_chat_id",  "TEXT DEFAULT ''"),
            ("alerts",       "note",              "TEXT DEFAULT ''"),
             ]:
            try:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
                await db.commit()
            except Exception:
                pass   # column already exists — ignore
        await db.commit()
    print(f"✓ Database ready at {DB_PATH}")


async def get_db():
    """Async context manager for DB access."""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        yield db
    finally:
        await db.close()
