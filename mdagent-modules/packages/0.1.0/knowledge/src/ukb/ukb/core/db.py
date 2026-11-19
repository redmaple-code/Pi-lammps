"""SQLite connection, schema bootstrap and write transactions."""
import contextlib
import os
import sqlite3

from .errors import UKBError

SCHEMA_VERSION = "2"
_JOURNAL_MODES = {"DELETE", "WAL", "TRUNCATE", "PERSIST"}
_SCHEMA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")


def connect(path, journal_mode="DELETE"):
    jm = str(journal_mode).upper()
    if jm not in _JOURNAL_MODES:
        raise UKBError("CONFIG_INVALID", "unsupported journal_mode", journal_mode=journal_mode)
    conn = sqlite3.connect(path, isolation_level=None, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA journal_mode=%s" % jm)
    return conn


def is_initialized(conn):
    row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'").fetchone()
    return row is not None


def init_schema(conn):
    if is_initialized(conn):
        raise UKBError("ALREADY_INITIALIZED", "database already has a schema")
    with open(_SCHEMA_FILE, encoding="utf-8") as f:
        conn.executescript(f.read())


def check_version(conn):
    if not is_initialized(conn):
        raise UKBError("NOT_INITIALIZED", "database has no UKB schema; run init first")
    row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    if row is None or row["value"] != SCHEMA_VERSION:
        raise UKBError("SCHEMA_MISMATCH", "schema version mismatch",
                       found=row["value"] if row else None, expected=SCHEMA_VERSION)
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise UKBError("CONFIG_INVALID", "foreign_keys must be enabled")


@contextlib.contextmanager
def tx(conn):
    if conn.in_transaction:
        raise UKBError("INTERNAL", "nested write transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
