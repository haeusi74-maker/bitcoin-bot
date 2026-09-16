from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    symbol TEXT NOT NULL,
    price REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    source TEXT NOT NULL,       -- z.B. "momentum", spaeter "discord:mo"
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,    -- "long" | "short" | "neutral"
    confidence REAL NOT NULL,   -- -1.0 .. 1.0
    reason TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    side TEXT NOT NULL,         -- "buy" | "sell"
    price REAL NOT NULL,
    amount_btc REAL NOT NULL,
    cash_after REAL NOT NULL,
    btc_after REAL NOT NULL,
    reason TEXT,
    fee REAL NOT NULL DEFAULT 0  -- in EUR, simuliert Exchange-Handelsgebuehr
);

CREATE TABLE IF NOT EXISTS portfolio_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    cash REAL NOT NULL,
    btc REAL NOT NULL,
    last_trade_ts REAL,
    avg_entry_price REAL NOT NULL DEFAULT 0  -- mengengewichteter Einstiegspreis der aktuellen BTC-Position, fuer Stop-Loss/Take-Profit
);
"""


@contextmanager
def get_conn(db_path: str):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: str, starting_cash: float) -> None:
    with get_conn(db_path) as conn:
        conn.executescript(SCHEMA)
        # Migration fuer DBs, die vor Einfuehrung der fee-Spalte angelegt wurden.
        trade_columns = [row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()]
        if "fee" not in trade_columns:
            conn.execute("ALTER TABLE trades ADD COLUMN fee REAL NOT NULL DEFAULT 0")

        # Migration fuer DBs, die vor Einfuehrung von avg_entry_price angelegt wurden.
        state_columns = [row[1] for row in conn.execute("PRAGMA table_info(portfolio_state)").fetchall()]
        if "avg_entry_price" not in state_columns:
            conn.execute("ALTER TABLE portfolio_state ADD COLUMN avg_entry_price REAL NOT NULL DEFAULT 0")
        row = conn.execute("SELECT id FROM portfolio_state WHERE id = 1").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO portfolio_state (id, cash, btc, last_trade_ts) VALUES (1, ?, 0, NULL)",
                (starting_cash,),
            )
        conn.commit()


def record_price(db_path: str, symbol: str, price: float, ts: float | None = None) -> None:
    ts = ts if ts is not None else time.time()
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO price_history (ts, symbol, price) VALUES (?, ?, ?)",
            (ts, symbol, price),
        )
        conn.commit()


def get_price_history(db_path: str, symbol: str, limit: int = 200) -> list[tuple[float, float]]:
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT ts, price FROM price_history WHERE symbol = ? ORDER BY ts DESC LIMIT ?",
            (symbol, limit),
        ).fetchall()
    return list(reversed(rows))


def record_signal(
    db_path: str,
    source: str,
    symbol: str,
    direction: str,
    confidence: float,
    reason: str = "",
    ts: float | None = None,
) -> None:
    ts = ts if ts is not None else time.time()
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO signals (ts, source, symbol, direction, confidence, reason) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ts, source, symbol, direction, confidence, reason),
        )
        conn.commit()


def get_recent_signals(db_path: str, symbol: str, since_ts: float) -> list[sqlite3.Row]:
    with get_conn(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM signals WHERE symbol = ? AND ts >= ? ORDER BY ts DESC",
            (symbol, since_ts),
        ).fetchall()
    return rows


def get_latest_signal_ts(db_path: str, source: str, symbol: str) -> float | None:
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT MAX(ts) FROM signals WHERE source = ? AND symbol = ?",
            (source, symbol),
        ).fetchone()
    return row[0] if row and row[0] is not None else None


def get_portfolio_state(db_path: str) -> dict:
    with get_conn(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM portfolio_state WHERE id = 1").fetchone()
    return dict(row)


def update_portfolio_state(
    db_path: str, cash: float, btc: float, last_trade_ts: float, avg_entry_price: float
) -> None:
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE portfolio_state SET cash = ?, btc = ?, last_trade_ts = ?, avg_entry_price = ? WHERE id = 1",
            (cash, btc, last_trade_ts, avg_entry_price),
        )
        conn.commit()


def record_trade(
    db_path: str,
    side: str,
    price: float,
    amount_btc: float,
    cash_after: float,
    btc_after: float,
    reason: str,
    fee: float = 0.0,
    ts: float | None = None,
) -> None:
    ts = ts if ts is not None else time.time()
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO trades (ts, side, price, amount_btc, cash_after, btc_after, reason, fee) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, side, price, amount_btc, cash_after, btc_after, reason, fee),
        )
        conn.commit()


def get_trades(db_path: str, limit: int = 100) -> list[sqlite3.Row]:
    with get_conn(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    return rows
