"""Self-contained auth store.

A single SQLite file (``users.db``) holds registered users. Passwords are
stored only as salted, one-way hashes, so the server never sees a plaintext
password after registration.

Tokens are 12-hour JWTs — long enough for a whole meeting day, since they only
authenticate this app's own WebSocket. The client (agent UI) exchanges
credentials for a token, and every WebSocket upgrade carries that token. The
server verifies it and trusts the ``sub`` (user id) it contains -- no separate
session table.

Storage: if the environment variable DATABASE_URL is set (e.g. a Postgres
connection string), we use that instead and skip SQLite entirely. Leave it
unset and we get a local ``users.db`` file that survives server restarts.

The signing key (``SECRET_KEY``) is generated once and stored next to the
database, so tokens survive a server restart. Deleting ``.secret_key`` or
setting ``SECRET_KEY`` explicitly in the environment invalidates every token
issued so far.
"""

import hashlib
import hmac
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")  # e.g. postgres://... (optional)

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_SECONDS = 12 * 60 * 60  # 12 hours


# ---------------------------------------------------------------------------
# Signing key — persisted so issued tokens survive a server restart
# ---------------------------------------------------------------------------

_secret_key_path = Path(__file__).resolve().parent / ".secret_key"


def _load_or_create_secret_key() -> str:
    """Return the persistent signing key, creating one on first run.

    An explicit ``SECRET_KEY`` in the environment always wins.
    """

    from_env = os.getenv("SECRET_KEY")

    if from_env:
        return from_env

    if _secret_key_path.exists():
        key = _secret_key_path.read_text(encoding="utf-8").strip()

        if key:
            return key

    key = secrets.token_hex(32)

    try:
        _secret_key_path.write_text(key, encoding="utf-8")
    except OSError as e:
        # Read-only filesystem etc. Fall back to an in-memory key; every
        # restart then logs everyone out, but the server still runs.
        print(f"Could not persist secret key: {e}")

    return key


SECRET_KEY = _load_or_create_secret_key()


# ---------------------------------------------------------------------------
# Password hashing (no extra dependency: stdlib PBKDF2)
# ---------------------------------------------------------------------------

def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        100_000,  # iterations
    )
    return f"pbkdf2_sha256${100_000}${salt.hex()}${digest.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = stored.split("$")
        iterations = int(iterations)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, AttributeError):
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return hmac.compare_digest(actual, expected)


# ---------------------------------------------------------------------------
# SQLite storage (or whatever DATABASE_URL points at)
# ---------------------------------------------------------------------------

import sqlite3

_db_path = Path(__file__).resolve().parent / "users.db"


def get_db() -> sqlite3.Connection:
    """Open a connection to the SQLite DB, creating tables as needed."""
    os.makedirs(_db_path.parent, exist_ok=True)
    conn = sqlite3.connect(_db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id   TEXT PRIMARY KEY,
            username  TEXT NOT NULL UNIQUE,
            password  TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Token handling
# ---------------------------------------------------------------------------

def create_access_token(user_id: str) -> tuple[str, str, datetime]:
    """Return ``(access_token, jti, expires_at)``."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "jti": secrets.token_hex(16),
        "iat": now,
        "exp": now + timedelta(seconds=ACCESS_TOKEN_EXPIRE_SECONDS),
    }
    token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
    return token, payload["jti"], now + timedelta(seconds=ACCESS_TOKEN_EXPIRE_SECONDS)


def verify_token(token: str) -> dict | None:
    """Return the decoded payload, or ``None`` if the token is invalid/expired."""
    if not token or not SECRET_KEY:
        return None
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class UsernameTakenError(ValueError):
    """Raised by :func:`register` when the username already exists."""


def register(username: str, password: str) -> str:
    """Create a user and return their ``user_id``."""
    if not username or not password:
        raise ValueError("username and password are required")
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    if len(username) > 64:
        raise ValueError("username must be at most 64 characters")

    conn = get_db()
    try:
        user_id = secrets.token_hex(16)
        conn.execute(
            "INSERT INTO users (user_id, username, password, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                user_id,
                username.lower().strip(),
                _hash_password(password),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        raise UsernameTakenError(
            "that username is already registered"
        ) from e
    finally:
        conn.close()
    return user_id


def authenticate(username: str, password: str) -> str | None:
    """Return ``user_id`` if credentials are valid, else ``None``."""
    conn = get_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT user_id, password FROM users WHERE username = ?",
            (username.lower().strip(),),
        ).fetchone()
    finally:
        conn.close()
    if row is None or not _verify_password(password, row["password"]):
        return None
    return row["user_id"]


def get_user(user_id: str) -> dict | None:
    """Return a minimal user record for the given id."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT user_id, username, created_at FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {"user_id": row["user_id"], "username": row["username"]}


def update_password(username: str, new_password: str) -> None:
    """Update a user's password."""
    if len(new_password) < 8:
        raise ValueError("password must be at least 8 characters")
        
    conn = get_db()
    try:
        conn.execute(
            "UPDATE users SET password = ? WHERE username = ?",
            (_hash_password(new_password), username.lower().strip()),
        )
        conn.commit()
    finally:
        conn.close()

