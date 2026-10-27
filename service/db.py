"""SQLite (WAL) persistence. One host, two processes (web + worker): every write is a short BEGIN IMMEDIATE
transaction and RPC calls never happen inside one. Postgres is a swap behind this module (triggers: recurring
SQLITE_BUSY, p95 write-acquire > 250 ms, or a second host). Backups use the online backup API, never a file copy.
Connections are per thread (`connect()` is cheap); never share one sqlite3 connection between threads.
There is no migration framework yet: schema edits during the pilot mean deleting the scratch service.db."""
import contextlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

from sleuth import config as C

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
  id TEXT PRIMARY KEY, created_at REAL NOT NULL, kind TEXT NOT NULL, chain_id INTEGER NOT NULL, subject TEXT NOT NULL,
  email TEXT, title TEXT, status TEXT NOT NULL, product TEXT, access_hash TEXT NOT NULL, incident_ts REAL,
  claimed_loss TEXT, closed_at REAL);
CREATE TABLE IF NOT EXISTS investigation_runs (
  id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id), kind TEXT NOT NULL, state TEXT NOT NULL,
  input_hash TEXT NOT NULL, collector_version TEXT NOT NULL, requires_entitlement INTEGER NOT NULL DEFAULT 0,
  leased_by TEXT, lease_expires_at REAL, heartbeat_at REAL, attempts INTEGER NOT NULL DEFAULT 0, budget_s REAL,
  created_at REAL NOT NULL, started_at REAL, finished_at REAL, result_json TEXT, error TEXT,
  UNIQUE (case_id, kind, input_hash, collector_version));
CREATE TABLE IF NOT EXISTS coverage_checkpoints (
  id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, collector TEXT NOT NULL, start_block INTEGER, end_block INTEGER,
  next_block INTEGER, boundary_hash TEXT, finality TEXT NOT NULL, status TEXT NOT NULL, gaps_json TEXT, updated_at REAL NOT NULL,
  UNIQUE (owner_id, collector, finality));
CREATE TABLE IF NOT EXISTS evidence_items (
  id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id), run_id TEXT, kind TEXT NOT NULL, key TEXT NOT NULL,
  block INTEGER, block_hash TEXT, finality TEXT NOT NULL, payload_json TEXT NOT NULL, content_hash TEXT NOT NULL,
  observed_at REAL NOT NULL, superseded_at REAL, UNIQUE (case_id, key));
CREATE TABLE IF NOT EXISTS assertions (
  id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id), run_id TEXT, subject TEXT NOT NULL, predicate TEXT NOT NULL,
  object TEXT, verdict TEXT NOT NULL, reason TEXT, rule_version TEXT NOT NULL, created_at REAL NOT NULL, superseded_at REAL);
CREATE TABLE IF NOT EXISTS monitoring_subscriptions (
  id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id), chain_id INTEGER NOT NULL, token TEXT NOT NULL,
  addresses_json TEXT NOT NULL, state TEXT NOT NULL, starts_at REAL NOT NULL, ends_at REAL NOT NULL, start_block INTEGER,
  channel TEXT NOT NULL DEFAULT 'file', destination TEXT,
  provisional_cursor INTEGER, provisional_hash TEXT, finalized_cursor INTEGER, finalized_hash TEXT,
  last_success_at REAL, last_error TEXT, lag_blocks INTEGER, next_due_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS notification_outbox (
  id TEXT PRIMARY KEY, case_id TEXT NOT NULL, dedup_key TEXT NOT NULL, channel TEXT NOT NULL, destination TEXT,
  payload_json TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
  claimed_at REAL, sent_at REAL, last_error TEXT, UNIQUE (case_id, dedup_key));
CREATE TABLE IF NOT EXISTS payments (
  id TEXT PRIMARY KEY, case_id TEXT REFERENCES cases(id), provider TEXT NOT NULL, product TEXT NOT NULL, amount TEXT NOT NULL,
  asset TEXT NOT NULL, network TEXT NOT NULL, pay_to TEXT, payer TEXT, provider_ref TEXT, tx_hash TEXT, state TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE, created_at REAL NOT NULL, updated_at REAL NOT NULL, notes TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS payments_settled_tx ON payments(network, tx_hash) WHERE tx_hash IS NOT NULL;
CREATE TABLE IF NOT EXISTS x402_replays (
  key TEXT PRIMARY KEY, payment_id TEXT NOT NULL, request_hash TEXT NOT NULL, collector_version TEXT NOT NULL,
  state TEXT NOT NULL, response_json TEXT, settle_ref TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
  UNIQUE (payment_id));
CREATE TABLE IF NOT EXISTS audit_events (
  id TEXT PRIMARY KEY, at REAL NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT, detail_json TEXT);
"""
SCHEMA_VERSION = 2  # bump on every schema edit; an older file is refused (no migrations in the pilot), never silently reused


class DbError(Exception):
    """Persistence failure the caller must surface (never swallowed into a 'no data' result)."""


def path():
    return Path(os.environ.get("SLEUTH_SERVICE_DB", C.DATA / "service.db"))


def connect(db_path=None):
    p = Path(db_path) if db_path else path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(p, timeout=5.0, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        have = conn.execute("PRAGMA user_version").fetchone()[0]
        fresh = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0] == 0
        if not fresh and have != SCHEMA_VERSION:  # CREATE IF NOT EXISTS cannot add columns or constraints to an old file
            conn.close()
            raise DbError(f"{p} has schema version {have}, this code needs {SCHEMA_VERSION}; no migration tool in the pilot: delete the scratch db")
        conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        return conn
    except sqlite3.Error as e:
        raise DbError(f"open {p}: {e}")


@contextlib.contextmanager
def tx(conn):
    """Short write transaction. BEGIN IMMEDIATE takes the write lock up front so two processes never deadlock
    mid-transaction; the caller must not call RPC inside."""
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as e:
        raise DbError(f"begin: {e}")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException as e:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        if isinstance(e, sqlite3.Error):  # callers see one persistence error type; constraint checks stay inside the body
            raise DbError(f"{type(e).__name__}: {e}") from e
        raise


def new_id():
    return uuid.uuid4().hex


def now():
    return time.time()


def dumps(o):
    """Canonical JSON (sorted keys, no whitespace) so content hashes are stable across processes."""
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def loads(s):
    return json.loads(s) if s else None


def audit(conn, actor, action, target=None, detail=None):
    conn.execute("INSERT INTO audit_events VALUES (?,?,?,?,?,?)", (new_id(), now(), actor, action, target, dumps(detail) if detail else None))


def backup(conn, dest):
    """Consistent online backup (WAL-safe). A plain file copy can miss committed pages still in the -wal file."""
    try:
        dst = sqlite3.connect(dest)
        with dst:
            conn.backup(dst)
        dst.close()
    except sqlite3.Error as e:
        raise DbError(f"backup to {dest}: {e}")
