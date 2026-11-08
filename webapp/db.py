"""SQLite access: schema + thin connection helper (stdlib sqlite3, WAL)."""
import sqlite3

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user',
    approved      INTEGER NOT NULL DEFAULT 1,
    email         TEXT,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS instruments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    market        TEXT NOT NULL CHECK (market IN
                  ('cn_stock','cn_index','us_stock','us_index')),
    code          TEXT NOT NULL,
    name          TEXT NOT NULL,
    sina_code     TEXT NOT NULL,
    secid         TEXT NOT NULL,
    ma250         REAL,
    ma250_asof    TEXT,
    last_price    REAL,
    weekly_macd   TEXT NOT NULL DEFAULT 'alive',
    volume_waived INTEGER NOT NULL DEFAULT 0,
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    UNIQUE (user_id, market, code)
);
CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    window       TEXT NOT NULL,
    decision     TEXT NOT NULL,
    reason       TEXT NOT NULL DEFAULT '',
    source       TEXT NOT NULL DEFAULT '',
    payload_json TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    ts            TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    user_id       INTEGER NOT NULL,
    instrument_id INTEGER,
    market        TEXT NOT NULL,
    code          TEXT NOT NULL,
    name          TEXT NOT NULL,
    price         REAL,
    ma250         REAL,
    dist_pct      REAL,
    day_change_pct REAL,
    volume_ratio  REAL,
    weekly_macd   TEXT,
    note          TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_user_ts ON events(user_id, ts);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_dedup
    ON events(run_id, event_type, code, user_id);
CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT
);
CREATE TABLE IF NOT EXISTS instrument_status (
    instrument_id  INTEGER PRIMARY KEY REFERENCES instruments(id) ON DELETE CASCADE,
    side           TEXT,
    status         TEXT,
    below_days     INTEGER,
    price          REAL,
    day_change_pct REAL,
    volume_ratio   REAL,
    sell_signal    TEXT,
    sell_note      TEXT,
    buy_signal     TEXT,
    buy_note       TEXT,
    st_macd        TEXT,
    st_rsi         TEXT,
    st_note        TEXT,
    updated_at     TEXT
);
"""


def connect():
    # FastAPI may run a request's sync dependency and endpoint in different
    # threadpool threads; access is always sequential, so allow cross-thread
    # use of this per-request connection.
    conn = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        # lightweight migration: columns added after first deployment
        cols = {r[1] for r in conn.execute("PRAGMA table_info(instrument_status)")}
        for col in ("buy_signal", "buy_note", "st_macd", "st_rsi", "st_note"):
            if col not in cols:
                conn.execute(f"ALTER TABLE instrument_status ADD COLUMN {col} TEXT")
        ucols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
        if "role" not in ucols:
            conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
        if "approved" not in ucols:
            conn.execute("ALTER TABLE users ADD COLUMN approved INTEGER NOT NULL DEFAULT 1")
        if "email" not in ucols:
            conn.execute("ALTER TABLE users ADD COLUMN email TEXT")
        # bootstrap admin: the earliest existing user becomes admin so a
        # deployed instance always has exactly one approver
        has_admin = conn.execute("SELECT 1 FROM users WHERE role = 'admin'").fetchone()
        if not has_admin:
            first = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
            if first:
                conn.execute("UPDATE users SET role = 'admin', approved = 1 WHERE id = ?",
                             (first["id"],))
        conn.commit()
    finally:
        conn.close()
