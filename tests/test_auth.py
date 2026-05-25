from __future__ import annotations

import sqlite3
from pathlib import Path

from whatsapp_photos import auth as auth_mod
from whatsapp_photos.ingest import ingest

from .fixtures import build_fixture


def _ingest_db(tmp_path: Path) -> Path:
    chat_db, media_root = build_fixture(tmp_path)
    out = tmp_path / "photos.db"
    ingest(chat_db, media_root, out)
    return out


def test_password_round_trip(tmp_path: Path) -> None:
    db = _ingest_db(tmp_path)
    conn = sqlite3.connect(db)
    try:
        assert auth_mod.has_password(conn) is False
        auth_mod.set_password(conn, "correct horse battery staple")
        assert auth_mod.has_password(conn) is True
        assert auth_mod.verify_password(conn, "correct horse battery staple") is True
        assert auth_mod.verify_password(conn, "wrong") is False
    finally:
        conn.close()


def test_password_clear(tmp_path: Path) -> None:
    db = _ingest_db(tmp_path)
    conn = sqlite3.connect(db)
    try:
        auth_mod.set_password(conn, "hunter2")
        auth_mod.clear_password(conn)
        assert auth_mod.has_password(conn) is False
        assert auth_mod.verify_password(conn, "hunter2") is False
    finally:
        conn.close()


def test_password_change(tmp_path: Path) -> None:
    db = _ingest_db(tmp_path)
    conn = sqlite3.connect(db)
    try:
        auth_mod.set_password(conn, "first")
        auth_mod.set_password(conn, "second")
        assert auth_mod.verify_password(conn, "first") is False
        assert auth_mod.verify_password(conn, "second") is True
    finally:
        conn.close()


def test_set_empty_password_rejected(tmp_path: Path) -> None:
    db = _ingest_db(tmp_path)
    conn = sqlite3.connect(db)
    try:
        try:
            auth_mod.set_password(conn, "")
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for empty password")
    finally:
        conn.close()


def test_password_preserved_across_reingest(tmp_path: Path) -> None:
    """Refreshing the archive from a new iPhone backup must not wipe
    the user's password — they have no way to recover the hash and
    re-setting it every refresh is hostile UX."""
    chat_db, media_root = build_fixture(tmp_path)
    out = tmp_path / "photos.db"
    ingest(chat_db, media_root, out)

    conn = sqlite3.connect(out)
    auth_mod.set_password(conn, "correct horse battery staple")
    conn.close()

    # Re-ingest into the same output path (the same source files; that
    # part is irrelevant to the test, what matters is that ingest()
    # replaces the DB).
    ingest(chat_db, media_root, out)

    conn = sqlite3.connect(out)
    try:
        assert auth_mod.has_password(conn) is True
        assert auth_mod.verify_password(conn, "correct horse battery staple") is True
        assert auth_mod.verify_password(conn, "wrong") is False
    finally:
        conn.close()
