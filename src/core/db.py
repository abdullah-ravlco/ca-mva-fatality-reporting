"""Postgres connection, schema migrations and the raw-document store.

The JSON files under data/ stay the pipeline's working files. When a database is configured, Postgres
gets, in addition:
  - documents: a raw copy of every JSON output (key = the file's path relative to data/), and
  - the tables in src/core/db_migrations/ (incidents, articles, people, next of kin, contacts,
    evidence...), filled after each stage by src/core/db_sync.py.

Connection (.env). Works with any Postgres server or tool: local, Docker, Postgres.app, Supabase,
Neon, RDS, Cloud SQL, Render, Railway...
  DATABASE_URL        postgresql://user:password@host:5432/dbname?sslmode=require
  or the standard libpq variables psql and pgAdmin use:
  PGHOST PGPORT PGDATABASE PGUSER PGPASSWORD PGSSLMODE (and PGSERVICE / PGPASSFILE)
  DB_SCHEMA           schema for the tables (default "public"; created if missing)
  DB_CONNECT_TIMEOUT  seconds to wait for a connection (default 10)
Without any of these, nothing here is used and the pipeline works with the JSON files only.
"""

import json
import logging
import os
import re
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).with_name("db_migrations")
SCHEMA_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

_connection = None
_ready = False


# --------------------------------------------------
# Settings
# --------------------------------------------------

def database_url():
    value = (os.getenv("DATABASE_URL") or "").strip()
    # A pasted "DATABASE_URL=postgresql://..." as the value is a common slip: accept it.
    while value.upper().startswith("DATABASE_URL="):
        value = value.split("=", 1)[1].strip()
    value = value.strip('"').strip("'").strip()
    # "user:password@host:5432/db" without the scheme: add it (libpq needs postgresql://).
    if value and "://" not in value and "=" not in value and "@" in value:
        value = "postgresql://" + value
    return value


def enabled():
    """A database is configured: DATABASE_URL, or PGHOST + PGDATABASE, or PGSERVICE."""
    if database_url():
        return True
    if (os.getenv("PGHOST") or "").strip() and (os.getenv("PGDATABASE") or "").strip():
        return True
    return bool((os.getenv("PGSERVICE") or "").strip())


def schema_name():
    name = (os.getenv("DB_SCHEMA") or "public").strip() or "public"
    if not SCHEMA_NAME_RE.match(name):
        raise ValueError(f"DB_SCHEMA must be a plain name (letters, digits, underscore): {name!r}")
    return name


def connect_timeout():
    value = (os.getenv("DB_CONNECT_TIMEOUT") or "").strip()
    return int(value) if value else 10


def describe():
    """'user@host:port/dbname' for logs, never the password."""
    try:
        from psycopg.conninfo import conninfo_to_dict

        info = conninfo_to_dict(database_url()) if database_url() else {}
    except Exception:
        info = {}
    host = info.get("host") or os.getenv("PGHOST") or "localhost"
    port = info.get("port") or os.getenv("PGPORT") or "5432"
    name = info.get("dbname") or os.getenv("PGDATABASE") or "?"
    user = info.get("user") or os.getenv("PGUSER") or ""
    return f"{user + '@' if user else ''}{host}:{port}/{name} (schema {schema_name()})"


# --------------------------------------------------
# Connection
# --------------------------------------------------

def _open():
    import psycopg  # imported here so the JSON-only mode works without the package

    # An empty conninfo makes libpq read PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD, PGSSLMODE...
    connection = psycopg.connect(database_url(), autocommit=True, connect_timeout=connect_timeout())
    # search_path is set with SET (not a startup option) so connection poolers such as PgBouncer
    # and the Supabase/Neon poolers accept it.
    connection.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema_name()}"')
    connection.execute(f'SET search_path TO "{schema_name()}", public')
    return connection


def connection():
    """The shared connection, with the schema migrated (once per process)."""
    global _connection, _ready
    if _connection is None or _connection.closed:
        _connection = _open()
        _ready = False
    if not _ready:
        apply_migrations(_connection)
        _ready = True
    return _connection


def reset():
    """Forget the connection (after an error, or between tests)."""
    global _connection, _ready
    if _connection is not None and not _connection.closed:
        _connection.close()
    _connection, _ready = None, False


def _run(sql, params=()):
    """Execute and return the cursor; reconnects once if the connection was dropped."""
    import psycopg

    try:
        return connection().execute(sql, params)
    except psycopg.OperationalError:
        reset()
        return connection().execute(sql, params)


@contextmanager
def transaction():
    """A cursor whose statements all commit together, or not at all."""
    conn = connection()
    with conn.transaction():
        with conn.cursor() as cursor:
            yield cursor


# --------------------------------------------------
# Migrations
# --------------------------------------------------

def migration_files():
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def apply_migrations(conn):
    """Run every migration in db_migrations/ that has not run yet, in name order, each in a transaction."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )
    done = {row[0] for row in conn.execute("SELECT name FROM schema_migrations").fetchall()}
    for path in migration_files():
        if path.name in done:
            continue
        with conn.transaction():
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
        logger.info("Database migration applied: %s", path.name)


# --------------------------------------------------
# Raw documents (a copy of every JSON output)
# --------------------------------------------------

def jsonb(value):
    from psycopg.types.json import Jsonb

    return Jsonb(value, dumps=lambda data: json.dumps(data, ensure_ascii=False, default=str))


def put(key, data):
    _run(
        "INSERT INTO documents (key, data, updated_at) VALUES (%s, %s, now()) "
        "ON CONFLICT (key) DO UPDATE SET data = EXCLUDED.data, updated_at = now()",
        (key, jsonb(data)),
    )


def get(key, default=None):
    row = _run("SELECT data FROM documents WHERE key = %s", (key,)).fetchone()
    return row[0] if row else default


def get_with_time(key):
    """(data, updated_at) of one document, or None when there is no such document."""
    row = _run("SELECT data, updated_at FROM documents WHERE key = %s", (key,)).fetchone()
    return (row[0], row[1]) if row else None


def keys(prefix=""):
    return [row[0] for row in _run("SELECT key FROM documents WHERE key LIKE %s ORDER BY key", (prefix + "%",)).fetchall()]


def server_version():
    return _run("SHOW server_version").fetchone()[0]
