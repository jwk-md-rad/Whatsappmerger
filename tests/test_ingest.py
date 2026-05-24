from __future__ import annotations

import sqlite3
from pathlib import Path

from whatsapp_photos.ingest import IngestOptions, ingest

from .fixtures import build_fixture


def _q(db: Path, sql: str, params: tuple = ()) -> list[tuple]:
    c = sqlite3.connect(db)
    try:
        return c.execute(sql, params).fetchall()
    finally:
        c.close()


def test_ingest_counts_by_type(tmp_path: Path) -> None:
    chat_db, media_root = build_fixture(tmp_path)
    out = tmp_path / "archive.db"
    report = ingest(chat_db, media_root, out)

    # 3 real images, plus text messages: 2 fallbacks from media that
    # wasn't an image (missing file + non-image file) and 2 pure-text
    # messages added in the fixture.
    assert report.images_inserted == 3
    assert report.texts_inserted == 4


def test_ingest_resolves_metadata(tmp_path: Path) -> None:
    chat_db, media_root = build_fixture(tmp_path)
    out = tmp_path / "archive.db"
    ingest(chat_db, media_root, out)

    rows = _q(
        out,
        "SELECT chat_jid, sender_name, is_from_me, body, width, height, mime_type, "
        "filename FROM messages WHERE type = 'image' ORDER BY sent_at",
    )
    # Alice's pizza photo
    assert rows[0][0] == "111@s.whatsapp.net"
    assert rows[0][1] == "Alice"
    assert rows[0][2] == 0
    assert rows[0][3] == "best pizza in town"
    assert rows[0][4] == 40 and rows[0][5] == 30
    assert rows[0][6] == "image/png"
    assert rows[0][7] == "alice-pizza.jpg"

    # My cat photo (from_me=1, sender_name=Me)
    assert rows[1][2] == 1
    assert rows[1][1] == "Me"

    # Group: Carol's beach
    assert rows[2][1] == "Carol"
    assert rows[2][0] == "333-group@g.us"


def test_ingest_text_messages(tmp_path: Path) -> None:
    chat_db, media_root = build_fixture(tmp_path)
    out = tmp_path / "archive.db"
    ingest(chat_db, media_root, out)

    rows = _q(
        out,
        "SELECT chat_jid, sender_name, body, media_path FROM messages "
        "WHERE type = 'text' ORDER BY sent_at",
    )
    # 4 text messages: Alice's missing-file fallback, Dave's non-image
    # fallback, plus the two pure-text messages.
    assert len(rows) == 4
    assert all(r[3] is None for r in rows)  # text messages have no media_path
    bodies = [r[2] for r in rows]
    assert "see you at the restaurant tonight" in bodies
    assert "anyone bringing sunscreen?" in bodies


def test_ingest_chats_table(tmp_path: Path) -> None:
    chat_db, media_root = build_fixture(tmp_path)
    out = tmp_path / "archive.db"
    ingest(chat_db, media_root, out)
    chats = _q(out, "SELECT jid, name, is_group FROM chats ORDER BY jid")
    jids = {c[0] for c in chats}
    # All four chats now show up (Dave's appears because his row falls
    # back to a text message instead of being skipped).
    assert "111@s.whatsapp.net" in jids
    assert "222@s.whatsapp.net" in jids
    assert "333-group@g.us" in jids
    assert "444@s.whatsapp.net" in jids
    assert ("333-group@g.us", "Beach Trip", 1) in chats


def test_ingest_idempotent_via_unique(tmp_path: Path) -> None:
    chat_db, media_root = build_fixture(tmp_path)
    out = tmp_path / "archive.db"
    ingest(chat_db, media_root, out)
    # Re-running should clobber and produce the same row count.
    ingest(chat_db, media_root, out)
    n = _q(out, "SELECT COUNT(*) FROM messages")[0][0]
    assert n == 7  # 3 images + 4 texts


def test_ingest_sha256_optional(tmp_path: Path) -> None:
    chat_db, media_root = build_fixture(tmp_path)
    out = tmp_path / "archive.db"
    ingest(chat_db, media_root, out, options=IngestOptions(compute_sha256=True))
    sha_rows = _q(out, "SELECT sha256 FROM messages WHERE type = 'image'")
    assert all(len(r[0]) == 64 for r in sha_rows)


def test_ingest_path_with_spaces(tmp_path: Path) -> None:
    """SQLite file:URIs break on raw spaces; we should percent-encode them."""
    spaced = tmp_path / "Mobile Documents" / "Whatsapp Jar"
    spaced.mkdir(parents=True)
    chat_db, media_root = build_fixture(spaced)
    out = spaced / "archive.db"
    report = ingest(chat_db, media_root, out)
    assert report.images_inserted == 3


def test_ingest_fts_index_built(tmp_path: Path) -> None:
    chat_db, media_root = build_fixture(tmp_path)
    out = tmp_path / "archive.db"
    ingest(chat_db, media_root, out)
    # FTS over body finds both image-captions and text-message bodies.
    rows = _q(
        out,
        "SELECT messages.id FROM messages "
        "JOIN messages_fts ON messages_fts.rowid = messages.id "
        "WHERE messages_fts MATCH ?",
        ('"pizza"*',),
    )
    # Alice's caption + Carol's caption mention pizza
    assert len(rows) == 2

    # Text-message body is searchable too.
    rows = _q(
        out,
        "SELECT messages.id FROM messages "
        "JOIN messages_fts ON messages_fts.rowid = messages.id "
        "WHERE messages_fts MATCH ?",
        ('"sunscreen"*',),
    )
    assert len(rows) == 1
