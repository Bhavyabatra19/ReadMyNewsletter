"""
Persistence + crypto for ReadMyNewsletter accounts.

Two storage backends, chosen automatically:

  * Postgres  — when DATABASE_URL (or POSTGRES_URL) is set. This is what you use
                on serverless hosts like Vercel, where the local filesystem is
                read-only/ephemeral. Provision a free Neon/Vercel Postgres and it
                just works.
  * SQLite    — otherwise. Great for local dev and single-box persistent hosts.

Three things are stored:
  * users        — email + hashed password
  * connections  — each user's inbox/AI settings; the two secrets (inbox
                   app-password + Anthropic API key) are ENCRYPTED at rest
  * digests      — snapshots of generated digest HTML

Secrets are AES-encrypted (Fernet) with a key derived from APP_SECRET; account
passwords are hashed (never reversible). On serverless you MUST set a stable
APP_SECRET (and a Postgres DATABASE_URL) or data won't survive between requests.
"""

import base64
import hashlib
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from cryptography.fernet import Fernet
from werkzeug.security import check_password_hash, generate_password_hash

# --------------------------------------------------------------------------- #
# Backend selection
# --------------------------------------------------------------------------- #

DATABASE_URL = (
    os.environ.get("DATABASE_URL")
    or os.environ.get("POSTGRES_URL")
    or os.environ.get("POSTGRES_PRISMA_URL")
    or ""
).strip()
IS_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))

# On Vercel the working dir is read-only; only /tmp is writable. Pick a writable
# default path for the SQLite fallback and the generated secret.
_ON_SERVERLESS = bool(os.environ.get("VERCEL") or os.environ.get("AWS_REGION"))
_DEFAULT_DB = (
    "/tmp/readmynewsletter.db"
    if _ON_SERVERLESS
    else os.path.join(os.path.dirname(os.path.abspath(__file__)), "readmynewsletter.db")
)
DB_PATH = os.environ.get("DATABASE_PATH", _DEFAULT_DB)


# --------------------------------------------------------------------------- #
# Secret material
# --------------------------------------------------------------------------- #

_SECRET_CACHE = None


def app_secret():
    """The master secret. From APP_SECRET if set; otherwise a random value
    persisted next to the database (best-effort) and cached for this process.
    ALWAYS set APP_SECRET explicitly on serverless / multi-instance hosts."""
    global _SECRET_CACHE
    if _SECRET_CACHE:
        return _SECRET_CACHE
    env = os.environ.get("APP_SECRET")
    if env:
        _SECRET_CACHE = env
        return env
    secret_dir = os.path.dirname(DB_PATH) or "."
    path = os.path.join(secret_dir, ".app_secret")
    try:
        if os.path.exists(path):
            with open(path) as fh:
                _SECRET_CACHE = fh.read().strip()
                return _SECRET_CACHE
    except OSError:
        pass
    value = base64.urlsafe_b64encode(os.urandom(32)).decode()
    try:
        with open(path, "w") as fh:
            fh.write(value)
        os.chmod(path, 0o600)
    except OSError:
        pass  # read-only FS: fall back to an in-memory secret for this process
    _SECRET_CACHE = value
    return value


def _fernet():
    key = base64.urlsafe_b64encode(hashlib.sha256(app_secret().encode()).digest())
    return Fernet(key)


def flask_secret_key():
    return hashlib.sha256(("flask:" + app_secret()).encode()).hexdigest()


def encrypt(value):
    if not value:
        return ""
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token):
    if not token:
        return ""
    return _fernet().decrypt(token.encode()).decode()


# --------------------------------------------------------------------------- #
# Database layer (works on both SQLite and Postgres)
# --------------------------------------------------------------------------- #

def _connect():
    if IS_PG:
        import psycopg
        from psycopg.rows import dict_row

        # Force UTF-8 decoding of TEXT columns regardless of server encoding.
        return psycopg.connect(
            DATABASE_URL, row_factory=dict_row, connect_timeout=10, client_encoding="UTF8"
        )
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _q(sql):
    """Translate SQLite '?' placeholders to Postgres '%s'."""
    return sql.replace("?", "%s") if IS_PG else sql


@contextmanager
def _db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _fetchone(conn, sql, params=()):
    cur = conn.execute(_q(sql), params)
    row = cur.fetchone()
    return dict(row) if row is not None else None


def _fetchall(conn, sql, params=()):
    cur = conn.execute(_q(sql), params)
    return [dict(r) for r in cur.fetchall()]


def _insert(conn, sql, params):
    """INSERT and return the new row id, on either backend."""
    if IS_PG:
        cur = conn.execute(_q(sql) + " RETURNING id", params)
        return cur.fetchone()["id"]
    cur = conn.execute(sql, params)
    return cur.lastrowid


def _pk():
    return "SERIAL PRIMARY KEY" if IS_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"


def init_db():
    stmts = [
        f"""CREATE TABLE IF NOT EXISTS users (
                id            {_pk()},
                email         TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at    TEXT NOT NULL
            )""",
        """CREATE TABLE IF NOT EXISTS connections (
                user_id       INTEGER PRIMARY KEY REFERENCES users(id),
                imap_host     TEXT NOT NULL,
                imap_port     INTEGER NOT NULL,
                imap_user     TEXT NOT NULL,
                imap_pass_enc TEXT NOT NULL,
                imap_folder   TEXT NOT NULL DEFAULT 'INBOX',
                api_key_enc   TEXT NOT NULL DEFAULT '',
                model         TEXT NOT NULL DEFAULT '',
                days          INTEGER NOT NULL DEFAULT 7,
                use_ai        INTEGER NOT NULL DEFAULT 1,
                include_read  INTEGER NOT NULL DEFAULT 0,
                auto_refresh  INTEGER NOT NULL DEFAULT 1,
                updated_at    TEXT NOT NULL
            )""",
        f"""CREATE TABLE IF NOT EXISTS digests (
                id          {_pk()},
                user_id     INTEGER NOT NULL REFERENCES users(id),
                created_at  TEXT NOT NULL,
                day_range   TEXT NOT NULL,
                item_count  INTEGER NOT NULL,
                total_time  INTEGER NOT NULL,
                html        TEXT NOT NULL
            )""",
        "CREATE INDEX IF NOT EXISTS idx_digests_user ON digests(user_id, created_at DESC)",
    ]
    with _db() as conn:
        for stmt in stmts:
            conn.execute(stmt)


def _now():
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #

def create_user(email, password):
    email = email.strip().lower()
    with _db() as conn:
        return _insert(
            conn,
            "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
            (email, generate_password_hash(password), _now()),
        )


def user_by_email(email):
    with _db() as conn:
        return _fetchone(conn, "SELECT * FROM users WHERE email = ?", (email.strip().lower(),))


def user_by_id(user_id):
    with _db() as conn:
        return _fetchone(conn, "SELECT * FROM users WHERE id = ?", (user_id,))


def verify_login(email, password):
    user = user_by_email(email)
    if user and check_password_hash(user["password_hash"], password):
        return user
    return None


# --------------------------------------------------------------------------- #
# Connections
# --------------------------------------------------------------------------- #

def save_connection(user_id, data):
    with _db() as conn:
        conn.execute(
            _q(
                """
            INSERT INTO connections
                (user_id, imap_host, imap_port, imap_user, imap_pass_enc,
                 imap_folder, api_key_enc, model, days, use_ai, include_read,
                 auto_refresh, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                imap_host=excluded.imap_host,
                imap_port=excluded.imap_port,
                imap_user=excluded.imap_user,
                imap_pass_enc=excluded.imap_pass_enc,
                imap_folder=excluded.imap_folder,
                api_key_enc=excluded.api_key_enc,
                model=excluded.model,
                days=excluded.days,
                use_ai=excluded.use_ai,
                include_read=excluded.include_read,
                auto_refresh=excluded.auto_refresh,
                updated_at=excluded.updated_at
            """
            ),
            (
                user_id,
                data["imap_host"],
                int(data["imap_port"]),
                data["imap_user"],
                encrypt(data["imap_password"]),
                data.get("imap_folder", "INBOX"),
                encrypt(data.get("api_key", "")),
                data.get("model", ""),
                int(data.get("days", 7)),
                1 if data.get("use_ai") else 0,
                1 if data.get("include_read") else 0,
                1 if data.get("auto_refresh", True) else 0,
                _now(),
            ),
        )


def get_connection(user_id, reveal=False):
    with _db() as conn:
        out = _fetchone(conn, "SELECT * FROM connections WHERE user_id = ?", (user_id,))
    if not out:
        return None
    out["use_ai"] = bool(out["use_ai"])
    out["include_read"] = bool(out["include_read"])
    out["auto_refresh"] = bool(out["auto_refresh"])
    out["has_api_key"] = bool(out["api_key_enc"])
    if reveal:
        out["imap_password"] = decrypt(out["imap_pass_enc"])
        out["api_key"] = decrypt(out["api_key_enc"])
    out.pop("imap_pass_enc", None)
    out.pop("api_key_enc", None)
    return out


def connections_for_auto_refresh():
    with _db() as conn:
        rows = _fetchall(conn, "SELECT user_id FROM connections WHERE auto_refresh = 1")
    return [r["user_id"] for r in rows]


# --------------------------------------------------------------------------- #
# Digests
# --------------------------------------------------------------------------- #

def add_digest(user_id, html, day_range, item_count, total_time):
    with _db() as conn:
        new_id = _insert(
            conn,
            """INSERT INTO digests
               (user_id, created_at, day_range, item_count, total_time, html)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, _now(), day_range, item_count, total_time, html),
        )
        conn.execute(
            _q(
                """DELETE FROM digests WHERE user_id = ? AND id NOT IN
                   (SELECT id FROM digests WHERE user_id = ?
                    ORDER BY created_at DESC LIMIT 30)"""
            ),
            (user_id, user_id),
        )
        return new_id


def list_digests(user_id):
    with _db() as conn:
        return _fetchall(
            conn,
            """SELECT id, created_at, day_range, item_count, total_time
               FROM digests WHERE user_id = ? ORDER BY created_at DESC""",
            (user_id,),
        )


def get_digest(user_id, digest_id):
    with _db() as conn:
        return _fetchone(
            conn, "SELECT * FROM digests WHERE id = ? AND user_id = ?", (digest_id, user_id)
        )


def latest_digest_time(user_id):
    with _db() as conn:
        row = _fetchone(
            conn,
            "SELECT created_at FROM digests WHERE user_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        )
    return row["created_at"] if row else None


# --------------------------------------------------------------------------- #
# Demo account (lets you try the app without connecting a real inbox)
# --------------------------------------------------------------------------- #

DEMO_EMAIL = "demo@readmynewsletter.app"
DEMO_PASSWORD = "demo1234"


def _demo_digest_html():
    """Use the bundled sample digest if present; otherwise a small placeholder.
    Must contain a <body> tag so the reader toolbar can be injected."""
    sample = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample_digest.html")
    try:
        with open(sample, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<title>Demo digest</title></head><body style='font-family:Georgia,serif;"
            "max-width:640px;margin:40px auto;padding:0 20px'>"
            "<h1>Your demo digest</h1><p>This is a sample. Connect your own inbox "
            "in Settings to get real newsletter summaries here.</p></body></html>"
        )


def ensure_demo():
    """Idempotently create the demo user + a seeded connection + sample digest,
    and return the demo user's id. Safe to call on every demo login."""
    user = user_by_email(DEMO_EMAIL)
    if user:
        uid = user["id"]
    else:
        try:
            uid = create_user(DEMO_EMAIL, DEMO_PASSWORD)
        except Exception:  # noqa: BLE001  (concurrent create -> unique clash)
            existing = user_by_email(DEMO_EMAIL)
            uid = existing["id"] if existing else None
            if uid is None:
                raise
    if not get_connection(uid):
        save_connection(uid, {
            "imap_host": "imap.example.com", "imap_port": 993, "imap_user": DEMO_EMAIL,
            "imap_password": "demo", "imap_folder": "INBOX", "api_key": "", "model": "",
            "days": 7, "use_ai": False, "include_read": False, "auto_refresh": False,
        })
    if not list_digests(uid):
        add_digest(uid, _demo_digest_html(), "last 7 days", 6, 24)
    return uid


# Initialise on import. Never let a transient DB hiccup crash app startup /
# import (which on serverless would surface as FUNCTION_INVOCATION_FAILED);
# routes re-run init on demand via ensure_db().
_DB_READY = False


def ensure_db():
    global _DB_READY
    if _DB_READY:
        return
    init_db()
    _DB_READY = True
    # Seed the demo account so both the button and the login form work.
    try:
        ensure_demo()
    except Exception as exc:  # noqa: BLE001  (never let seeding break real logins)
        print(f"[store] demo seed skipped: {exc}")


try:
    ensure_db()
except Exception as exc:  # noqa: BLE001
    print(f"[store] deferred DB init (will retry on first request): {exc}")
