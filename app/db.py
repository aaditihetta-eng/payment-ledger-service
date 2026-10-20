"""SQLite helpers: one connection per request and an explicit transaction helper.

Money is stored as integer paise (1 rupee = 100 paise) to avoid float rounding errors.
"""
import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("LEDGER_DB", "ledger.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    balance_paise INTEGER NOT NULL CHECK (balance_paise >= 0),
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS transfers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    from_account    INTEGER NOT NULL REFERENCES accounts(id),
    to_account      INTEGER NOT NULL REFERENCES accounts(id),
    amount_paise    INTEGER NOT NULL CHECK (amount_paise > 0),
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Every balance change has a ledger row, so balances can always be re-derived.
CREATE TABLE IF NOT EXISTS ledger_entries (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id     INTEGER NOT NULL REFERENCES accounts(id),
    transfer_id    INTEGER REFERENCES transfers(id),
    delta_paise    INTEGER NOT NULL,
    kind           TEXT NOT NULL CHECK (kind IN ('deposit', 'transfer_out', 'transfer_in')),
    created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def get_conn() -> sqlite3.Connection:
    # isolation_level=None -> we control BEGIN/COMMIT ourselves.
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db() -> None:
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE takes the write lock up front, so two transfers can never
    read the same balance and both spend it (no lost updates)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
