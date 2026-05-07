"""Password storage and verification.

The password is stored as a PBKDF2-HMAC-SHA256 hash with a per-DB random
salt in a ``meta`` key/value table. We use the stdlib (``hashlib``,
``secrets``, ``hmac``) so there are no extra dependencies.

The password gates *web access* only — anyone with read access to the
.db file can still query it directly. For at-rest encryption you'd need
sqlcipher; that's a different scope.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3


PBKDF2_ITERATIONS = 200_000
SALT_BYTES = 16
HASH_BYTES = 32


def init_meta(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value BLOB)"
    )


def set_password(conn: sqlite3.Connection, password: str) -> None:
    """Replace any existing password with a hash of ``password``."""
    if not password:
        raise ValueError("password must be non-empty")
    init_meta(conn)
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS, HASH_BYTES
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('auth.salt', ?)",
        (salt,),
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('auth.hash', ?)",
        (digest,),
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) "
        "VALUES ('auth.iterations', ?)",
        (str(PBKDF2_ITERATIONS).encode(),),
    )
    conn.commit()


def clear_password(conn: sqlite3.Connection) -> None:
    init_meta(conn)
    conn.execute("DELETE FROM meta WHERE key IN ('auth.salt','auth.hash','auth.iterations')")
    conn.commit()


def _meta_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()
    return row is not None


def has_password(conn: sqlite3.Connection) -> bool:
    if not _meta_exists(conn):
        return False
    row = conn.execute("SELECT 1 FROM meta WHERE key = 'auth.hash'").fetchone()
    return row is not None


def verify_password(conn: sqlite3.Connection, password: str) -> bool:
    """Constant-time check of ``password`` against the stored hash."""
    if not _meta_exists(conn):
        return False
    row = conn.execute(
        "SELECT "
        "  (SELECT value FROM meta WHERE key='auth.salt'), "
        "  (SELECT value FROM meta WHERE key='auth.hash'), "
        "  (SELECT value FROM meta WHERE key='auth.iterations')"
    ).fetchone()
    if not row or row[0] is None or row[1] is None:
        return False
    salt, expected = row[0], row[1]
    iters = int(row[2] or PBKDF2_ITERATIONS)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iters, len(expected)
    )
    return hmac.compare_digest(digest, expected)
