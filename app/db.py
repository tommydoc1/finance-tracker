"""SQLite storage. One file, lives in the /data volume so container updates never touch it."""
import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", "/data/finance.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,              -- stocks_isa | cash_isa | lisa | cash | pension | sipp | gia
    provider TEXT DEFAULT '',
    valuation_mode TEXT NOT NULL DEFAULT 'holdings',  -- holdings (cash + units x price) | manual (statement values)
    opened_on TEXT,
    notes TEXT DEFAULT '',
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS holdings (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    ticker TEXT NOT NULL,
    name TEXT DEFAULT '',
    isin TEXT DEFAULT '',
    ocf_pct REAL DEFAULT 0,          -- ongoing charge / TER, % per year
    price_symbol TEXT DEFAULT '',    -- Yahoo symbol for auto prices, e.g. VUAG.L ; blank = manual prices
    notes TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS prices (
    holding_id INTEGER NOT NULL REFERENCES holdings(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    price REAL NOT NULL,             -- GBP per unit
    source TEXT DEFAULT 'manual',
    PRIMARY KEY (holding_id, date)
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    type TEXT NOT NULL,              -- deposit | withdrawal | contribution | tax_relief | transfer_in | transfer_out
                                     -- buy | sell | dividend | interest | fee | bonus | opening_balance
                                     -- unit_adjust (amount 0, units +/-: correct unit count to match provider)
    amount REAL NOT NULL,            -- always positive GBP; direction comes from type
    holding_id INTEGER REFERENCES holdings(id) ON DELETE SET NULL,
    units REAL,
    notes TEXT DEFAULT '',
    recurring_id INTEGER REFERENCES recurring(id) ON DELETE SET NULL,
    recurring_key TEXT,
    external_id TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_txn_recurring ON transactions(recurring_id, recurring_key);
CREATE UNIQUE INDEX IF NOT EXISTS ux_txn_external ON transactions(account_id, external_id);
CREATE INDEX IF NOT EXISTS ix_txn_account_date ON transactions(account_id, date);

-- Time-varying rates: interest AER, platform fee %, flat monthly fee. Each row applies from effective_date onward.
CREATE TABLE IF NOT EXISTS rates (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,              -- interest | fee_pct | fee_flat
    effective_date TEXT NOT NULL,
    value REAL NOT NULL,             -- % for interest/fee_pct, GBP per month for fee_flat
    notes TEXT DEFAULT ''
);

-- Manual valuations (pension statements etc.)
CREATE TABLE IF NOT EXISTS valuations (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    value REAL NOT NULL,
    notes TEXT DEFAULT ''
);

-- Recurring payments (pension contributions, monthly ISA deposits)
CREATE TABLE IF NOT EXISTS recurring (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    txn_type TEXT NOT NULL DEFAULT 'contribution',
    rule TEXT NOT NULL DEFAULT 'last_working_day',  -- last_working_day | first_working_day | day_or_before | day_or_after
    day INTEGER DEFAULT 28,
    start_date TEXT NOT NULL,
    end_date TEXT,
    tax_relief_pct REAL DEFAULT 0,   -- e.g. 25 for relief-at-source pensions: adds a tax_relief txn
    invest_holding_id INTEGER REFERENCES holdings(id) ON DELETE SET NULL,  -- auto-buy this fund with each payment
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS recurring_amounts (
    id INTEGER PRIMARY KEY,
    recurring_id INTEGER NOT NULL REFERENCES recurring(id) ON DELETE CASCADE,
    effective_from TEXT NOT NULL,
    amount REAL NOT NULL,
    notes TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS recurring_skips (
    recurring_id INTEGER NOT NULL REFERENCES recurring(id) ON DELETE CASCADE,
    recurring_key TEXT NOT NULL,
    PRIMARY KEY (recurring_id, recurring_key)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    return con


@contextmanager
def db():
    con = connect()
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


MIGRATIONS = [
    # (table, column, definition) – added to existing databases on startup
    ("recurring", "invest_holding_id", "INTEGER REFERENCES holdings(id) ON DELETE SET NULL"),
    ("holdings", "price_info", "TEXT"),
    ("accounts", "interest_mode", "TEXT NOT NULL DEFAULT 'manual'"),  # manual | daily (auto-credit, compounding)
    ("accounts", "interest_auto_from", "TEXT"),                        # first day auto-interest is calculated for  # what the price symbol matched, e.g. "Invesco EQQQ NASDAQ-100 UCITS ETF · LSE · GBp"
]


def init():
    with db() as con:
        con.executescript(SCHEMA)
        for table, col, ddl in MIGRATIONS:
            cols = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def rows(cur):
    return [dict(r) for r in cur.fetchall()]


def get_setting(con, key, default=None):
    r = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(con, key, value):
    con.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
