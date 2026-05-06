from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from whatsapp_merger.merger import merge_databases
from whatsapp_merger.schema import detect_flavor

from .fixtures import make_legacy_db, make_modern_db


def _query(db: Path, sql: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def test_modern_merge_dedupes_and_remaps(tmp_path: Path) -> None:
    a = tmp_path / "a.db"
    b = tmp_path / "b.db"
    out = tmp_path / "merged.db"
    make_modern_db(a, dataset="A")
    make_modern_db(b, dataset="B")

    report = merge_databases(a, b, out)

    assert report.flavor == "modern"
    # B introduced KEY_B2 (Bob) and KEY_C1 (Carol). KEY_B1 already in A.
    assert report.messages_inserted == 2
    assert report.messages_skipped_duplicate == 1
    # JIDs: Carol is new (Bob, me already in A). 1 added.
    assert report.jids_inserted == 1
    # Chats: chat with Carol is new. 1 added.
    assert report.chats_inserted == 1

    # Verify the messages are present and correctly attached to their chats.
    rows = _query(
        out,
        "SELECT m.key_id, m.text_data, j.raw_string "
        "FROM message m "
        "JOIN chat c ON c._id = m.chat_row_id "
        "JOIN jid j ON j._id = c.jid_row_id "
        "ORDER BY m.key_id",
    )
    assert ("KEY_A1", "Hello from Alice (A)", "111@s.whatsapp.net") in rows
    assert ("KEY_B2", "New from me to Bob", "222@s.whatsapp.net") in rows
    assert ("KEY_C1", "From Carol", "333@s.whatsapp.net") in rows

    # Quoted-row remap: KEY_C1 should quote the new dst _id of KEY_B2.
    quoted = _query(
        out,
        "SELECT m_q.key_id FROM message m "
        "JOIN message m_q ON m_q._id = m.quoted_row_id "
        "WHERE m.key_id = 'KEY_C1'",
    )
    assert quoted == [("KEY_B2",)]


def test_modern_merge_preserves_satellites(tmp_path: Path) -> None:
    a = tmp_path / "a.db"
    b = tmp_path / "b.db"
    out = tmp_path / "merged.db"
    make_modern_db(a, dataset="A")
    make_modern_db(b, dataset="B")
    merge_databases(a, b, out)

    # message_media row from A should still resolve to the right message.
    rows = _query(
        out,
        "SELECT m.key_id, mm.file_path FROM message_media mm "
        "JOIN message m ON m._id = mm.message_row_id",
    )
    assert rows == [("KEY_B1", "/Media/img-A.jpg")]


def test_modern_merge_strict_does_not_overwrite(tmp_path: Path) -> None:
    a = tmp_path / "a.db"
    b = tmp_path / "overlap.db"
    out = tmp_path / "merged.db"
    make_modern_db(a, dataset="A")
    make_modern_db(b, dataset="overlap")
    report = merge_databases(a, b, out, prefer_newer=False)

    assert report.messages_inserted == 0
    assert report.messages_skipped_duplicate == 1
    text = _query(out, "SELECT text_data FROM message WHERE key_id='KEY_A1'")
    assert text == [("Hello from Alice (A)",)]


def test_modern_merge_prefer_newer_overwrites(tmp_path: Path) -> None:
    a = tmp_path / "a.db"
    b = tmp_path / "overlap.db"
    out = tmp_path / "merged.db"
    make_modern_db(a, dataset="A")
    make_modern_db(b, dataset="overlap")
    report = merge_databases(a, b, out, prefer_newer=True)

    assert report.messages_inserted == 0
    text = _query(out, "SELECT text_data FROM message WHERE key_id='KEY_A1'")
    assert text == [("Hello from Alice (newer)",)]


def test_legacy_merge(tmp_path: Path) -> None:
    a = tmp_path / "a.db"
    b = tmp_path / "b.db"
    out = tmp_path / "merged.db"
    make_legacy_db(a, dataset="A")
    make_legacy_db(b, dataset="B")
    report = merge_databases(a, b, out)

    assert report.flavor == "legacy"
    assert report.messages_inserted == 1
    rows = _query(
        out,
        "SELECT key_remote_jid, key_id, data FROM messages ORDER BY key_id",
    )
    assert rows == [
        ("111@s.whatsapp.net", "L1", "legacy A1"),
        ("111@s.whatsapp.net", "L2", "legacy A2"),
        ("222@s.whatsapp.net", "L3", "legacy B3"),
    ]


def test_flavor_mismatch_raises(tmp_path: Path) -> None:
    a = tmp_path / "a.db"
    b = tmp_path / "b.db"
    out = tmp_path / "merged.db"
    make_modern_db(a, dataset="A")
    make_legacy_db(b, dataset="A")
    with pytest.raises(ValueError, match="Schema flavor mismatch"):
        merge_databases(a, b, out)


def test_idempotent_merge(tmp_path: Path) -> None:
    a = tmp_path / "a.db"
    b = tmp_path / "b.db"
    once = tmp_path / "once.db"
    twice = tmp_path / "twice.db"
    make_modern_db(a, dataset="A")
    make_modern_db(b, dataset="B")
    merge_databases(a, b, once)
    report = merge_databases(once, b, twice)
    # Second merge should add nothing — every src row already in dst.
    assert report.messages_inserted == 0
    assert report.jids_inserted == 0
    assert report.chats_inserted == 0


def test_detect_flavor_on_synthetic(tmp_path: Path) -> None:
    a = tmp_path / "a.db"
    make_modern_db(a, dataset="A")
    conn = sqlite3.connect(a)
    try:
        flavor = detect_flavor(conn)
        assert flavor.name == "modern"
    finally:
        conn.close()
