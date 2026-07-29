"""
Persistence + crypto for ReadMyNewsletter accounts.

SQLite (stdlib) holds three things:
  * users        — email + hashed password
  * connections  — each user's inbox/AI settings; the two secrets
                   (inbox app-password and Anthropic API key) are ENCRYPTED
                   at rest with Fernet
  * digests      — snapshots of generated digest HTML so a returning user
                   sees their latest reading room without reconnecting

Why store secrets at all? Because the whole point of the account mode is a
daily, unattended refresh: the background worker has to be able to log into the
inbox and call the API while you're asleep. Secrets are AES-encrypted (Fernet)
using a key derived from APP_SECRET; passwords are hashed (never reversible).
For a zero-storage experience, use the stateless CLI (newsletter_digest.py)
instead.
"""

import base64
import hashlib
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from cryptography.fernet import Fernet
from werkzeug.security import check_password_hash, generate_password_hash

DB_PATH = os.environ.get(
    "DATABASE_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "readmynewsletter.db"),
)


# --------------------------------------------------------------------------- #
# Secret material
# --------------------------------------------------------------------------- #

def app_secret():
    """The master secret. From APP_SECRET if set; otherwise a random value
    persisted next to the database so it survives restarts on a host with a
    volume. Set APP_SECRET explicitly in production."""
    env = os.environ.get("APP_SECRET")
    if env:
        return env
    path = os.path.join(os.path.dirname(DB_PATH) or ".", ".app_secret")
    if os.path.exists(path):
        with open(path) as fh:
            return fh.read().strip()
    value = base64.urlsafe_b64encode(os.urandom(32)).decode()
    with open(path, "w") as fh:
        fh.write(value)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return value


def _fernet():
    key = base64.urlsafe_b64encode(hashlib.sha256(app_secret().encode()).digest())
    return Fernet(key)


def flask_secret_key():
    """A distinct key for signing Flask session cookies."""
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
# Database
# --------------------------------------------------------------------------- #

@contextmanager
def _db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                email         TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at    TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS connections (
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
            );
            CREATE TABLE IF NOT EXISTS digests (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL REFERENCES users(id),
                created_at  TEXT NOT NULL,
                day_range   TEXT NOT NULL,
                item_count  INTEGER NOT NULL,
                total_time  INTEGER NOT NULL,
                html        TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_digests_user
                ON digests(user_id, created_at DESC);
            """
        )


def _now():
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #

def create_user(email, password):
    email = email.strip().lower()
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
            (email, generate_password_hash(password), _now()),
        )
        return cur.lastrowid


def user_by_email(email):
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE email = ?", (email.strip().lower(),)
        ).fetchone()
        return dict(row) if row else None


def user_by_id(user_id):
    with _db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def verify_login(email, password):
    user = user_by_email(email)
    if user and check_password_hash(user["password_hash"], password):
        return user
    return None


# --------------------------------------------------------------------------- #
# Connections
# --------------------------------------------------------------------------- #

def save_connection(user_id, data):
    """data: plain dict with imap_password / api_key in the clear; encrypted here."""
    with _db() as conn:
        conn.execute(
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
            """,
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
    """Return the connection. Secrets are only decrypted when reveal=True
    (i.e. when we are about to actually use them to fetch mail)."""
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM connections WHERE user_id = ?", (user_id,)
        ).fetchone()
    if not row:
        return None
    out = dict(row)
    out["use_ai"] = bool(out["use_ai"])
    out["include_read"] = bool(out["include_read"])
    out["auto_refresh"] = bool(out["auto_refresh"])
    out["has_api_key"] = bool(out["api_key_enc"])
    if reveal:
        out["imap_password"] = decrypt(out["imap_pass_enc"])
        out["api_key"] = decrypt(out["api_key_enc"])
    # Never leak the ciphertext to callers/templates.
    out.pop("imap_pass_enc", None)
    out.pop("api_key_enc", None)
    return out


def connections_for_auto_refresh():
    with _db() as conn:
        rows = conn.execute(
            "SELECT user_id FROM connections WHERE auto_refresh = 1"
        ).fetchall()
    return [r["user_id"] for r in rows]


# --------------------------------------------------------------------------- #
# Digests
# --------------------------------------------------------------------------- #

def add_digest(user_id, html, day_range, item_count, total_time):
    with _db() as conn:
        cur = conn.execute(
            """INSERT INTO digests
               (user_id, created_at, day_range, item_count, total_time, html)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, _now(), day_range, item_count, total_time, html),
        )
        # Keep only the most recent 30 per user.
        conn.execute(
            """DELETE FROM digests WHERE user_id = ? AND id NOT IN
               (SELECT id FROM digests WHERE user_id = ?
                ORDER BY created_at DESC LIMIT 30)""",
            (user_id, user_id),
        )
        return cur.lastrowid


def list_digests(user_id):
    with _db() as conn:
        rows = conn.execute(
            """SELECT id, created_at, day_range, item_count, total_time
               FROM digests WHERE user_id = ? ORDER BY created_at DESC""",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_digest(user_id, digest_id):
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM digests WHERE id = ? AND user_id = ?",
            (digest_id, user_id),
        ).fetchone()
    return dict(row) if row else None


def latest_digest_time(user_id):
    with _db() as conn:
        row = conn.execute(
            "SELECT created_at FROM digests WHERE user_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    return row["created_at"] if row else None


# Initialise on import so the app and worker share a ready database.
init_db()
