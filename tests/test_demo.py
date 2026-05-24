from __future__ import annotations

import sqlite3
from pathlib import Path

from whatsapp_photos.demo import PHOTO_COUNT, build_demo_fixture
from whatsapp_photos.demo import _DEMO_TEXTS  # type: ignore
from whatsapp_photos.ingest import ingest


def test_build_demo_fixture_files_exist(tmp_path: Path) -> None:
    chat_db, media_root = build_demo_fixture(tmp_path / "demo")
    assert chat_db.is_file()
    assert media_root.is_dir()
    media_files = list((media_root / "Message" / "Media").rglob("*.jpg"))
    assert len(media_files) == PHOTO_COUNT


def test_build_demo_fixture_is_idempotent(tmp_path: Path) -> None:
    chat_db1, _ = build_demo_fixture(tmp_path / "demo")
    chat_db2, _ = build_demo_fixture(tmp_path / "demo")
    assert chat_db1 == chat_db2
    assert chat_db1.is_file()


def test_demo_fixture_is_ingestible(tmp_path: Path) -> None:
    chat_db, media_root = build_demo_fixture(tmp_path / "demo")
    out = tmp_path / "archive.db"
    report = ingest(chat_db, media_root, out)
    assert report.images_inserted == PHOTO_COUNT
    assert report.texts_inserted == len(_DEMO_TEXTS)
    assert report.rows_skipped_missing_file == 0
    assert report.rows_skipped_unreadable == 0


def test_demo_fixture_has_groups_and_one_on_ones(tmp_path: Path) -> None:
    chat_db, media_root = build_demo_fixture(tmp_path / "demo")
    photos_db = tmp_path / "archive.db"
    ingest(chat_db, media_root, photos_db)

    conn = sqlite3.connect(photos_db)
    try:
        groups = conn.execute("SELECT COUNT(*) FROM chats WHERE is_group = 1").fetchone()[0]
        ones = conn.execute("SELECT COUNT(*) FROM chats WHERE is_group = 0").fetchone()[0]
    finally:
        conn.close()
    assert groups >= 1
    assert ones >= 1


def test_demo_fixture_search_works(tmp_path: Path) -> None:
    """Smoke-test that the FTS index is exercised by realistic content."""
    chat_db, media_root = build_demo_fixture(tmp_path / "demo")
    out = tmp_path / "archive.db"
    ingest(chat_db, media_root, out)

    conn = sqlite3.connect(out)
    try:
        rows = conn.execute(
            "SELECT messages.body, messages.type FROM messages "
            "JOIN messages_fts ON messages_fts.rowid = messages.id "
            "WHERE messages_fts MATCH 'pizza'"
        ).fetchall()
    finally:
        conn.close()
    # 'pizza' shows up in both captions (Alice's pizza-night image) and
    # text messages (Alice's "the pizza place around the corner").
    assert any("pizza" in r[0].lower() for r in rows)
    types = {r[1] for r in rows}
    assert "image" in types and "text" in types
