import threading
import time
import psycopg2
import psycopg2.extras
from psycopg2 import pool as pg_pool
from pathlib import Path

import boto3

from config import Config

SCHEMA_FILE = Path(__file__).resolve().parent / "migrations" / "schema.sql"

# ── IAM token cache ───────────────────────────────────────────────────────────
# Tokens are valid 15 min; we refresh at 14 min so we never send an expired one.

_rds_client = None
_cached_token: str | None = None
_token_expires: float = 0.0
_token_lock = threading.Lock()


def _get_rds_client():
    global _rds_client
    if _rds_client is None:
        _rds_client = boto3.client("rds", region_name=Config.AWS_REGION)
    return _rds_client


def _auth_token() -> str:
    """Return a cached IAM auth token, refreshing only when near expiry."""
    global _cached_token, _token_expires
    with _token_lock:
        if _cached_token is None or time.monotonic() >= _token_expires:
            _cached_token = _get_rds_client().generate_db_auth_token(
                DBHostname=Config.RDS_HOST,
                Port=Config.RDS_PORT,
                DBUsername=Config.RDS_USER,
            )
            _token_expires = time.monotonic() + 840  # 14 min
        return _cached_token


# ── Connection pool ───────────────────────────────────────────────────────────
# Pool reuses existing TCP connections (no handshake overhead per request).
# New connections within the pool use the current cached token.

_pool: pg_pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


def _get_pool() -> pg_pool.ThreadedConnectionPool:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = pg_pool.ThreadedConnectionPool(
                minconn=1,
                maxconn=10,
                host=Config.RDS_HOST,
                port=Config.RDS_PORT,
                dbname=Config.RDS_DB,
                user=Config.RDS_USER,
                password=_auth_token(),
                sslmode="require",
                connect_timeout=10,
            )
    return _pool


def _checkout() -> psycopg2.extensions.connection:
    """Get a healthy connection from the pool, replacing it if it's broken."""
    pool = _get_pool()
    conn = pool.getconn()
    if conn.closed:
        # Replace dead connection
        try:
            pool.putconn(conn)
        except Exception:
            pass
        conn = psycopg2.connect(
            host=Config.RDS_HOST,
            port=Config.RDS_PORT,
            dbname=Config.RDS_DB,
            user=Config.RDS_USER,
            password=_auth_token(),
            sslmode="require",
            connect_timeout=10,
        )
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    return conn


def get_db():
    """
    Return a per-request psycopg2 connection stored in Flask's g object.
    Connections are pooled and reused across requests for speed.
    Returned to the pool at end of request via close_db().
    """
    from flask import g

    if "db" not in g:
        g.db = _checkout()
    return g.db


def close_db(e=None):
    """Return the connection to the pool (registered as teardown_appcontext)."""
    from flask import g

    conn = g.pop("db", None)
    if conn is not None:
        try:
            if not conn.closed:
                conn.rollback()  # reset any uncommitted transaction state
                _get_pool().putconn(conn)
            else:
                _get_pool().putconn(conn)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass


def init_schema():
    """Run the DDL schema file once at application startup to create tables."""
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    conn = _checkout()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    finally:
        _get_pool().putconn(conn)
