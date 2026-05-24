from __future__ import annotations

import sqlite3
from pathlib import Path

from whatsapp_photos.ingest import ingest
from whatsapp_photos.search import (
    SearchFilters,
    count_messages,
    list_chats,
    list_senders,
    search_messages,
)

from .fixtures import build_fixture


def _ingested(tmp_path: Path) -> sqlite3.Connection:
    chat_db, media_root = build_fixture(tmp_path)
    out = tmp_path / "archive.db"
    ingest(chat_db, media_root, out)
    return sqlite3.connect(f"file:{out}?mode=ro", uri=True)


def test_search_by_caption(tmp_path: Path) -> None:
    conn = _ingested(tmp_path)
    hits = search_messages(conn, SearchFilters(query="pizza"))
    assert {h.chat_jid for h in hits} == {"111@s.whatsapp.net", "333-group@g.us"}
    assert any("<mark>pizza</mark>" in (h.snippet or "") for h in hits)


def test_search_text_body(tmp_path: Path) -> None:
    conn = _ingested(tmp_path)
    hits = search_messages(conn, SearchFilters(query="sunscreen"))
    assert len(hits) == 1
    assert hits[0].type == "text"
    assert hits[0].sender_name == "Carol"


def test_search_by_sender_name(tmp_path: Path) -> None:
    conn = _ingested(tmp_path)
    hits = search_messages(conn, SearchFilters(query="Carol"))
    assert {h.chat_jid for h in hits} == {"333-group@g.us"}


def test_search_filter_chat(tmp_path: Path) -> None:
    conn = _ingested(tmp_path)
    chats = list_chats(conn)
    bob = next(c for c in chats if c["jid"] == "222@s.whatsapp.net")
    hits = search_messages(conn, SearchFilters(chat_id=bob["id"]))
    assert len(hits) == 1
    assert hits[0].is_from_me is True


def test_search_filter_type(tmp_path: Path) -> None:
    conn = _ingested(tmp_path)
    images = search_messages(conn, SearchFilters(type="image"))
    texts = search_messages(conn, SearchFilters(type="text"))
    assert len(images) == 3
    assert len(texts) == 4


def test_search_date_range(tmp_path: Path) -> None:
    conn = _ingested(tmp_path)
    hits = search_messages(
        conn,
        SearchFilters(since_unix=1_699_999_000, until_unix=1_700_005_000),
    )
    assert len(hits) == 1
    assert hits[0].chat_jid == "111@s.whatsapp.net"


def test_search_count_matches(tmp_path: Path) -> None:
    conn = _ingested(tmp_path)
    assert count_messages(conn, SearchFilters()) == 7
    assert count_messages(conn, SearchFilters(query="pizza")) == 2
    assert count_messages(conn, SearchFilters(type="image")) == 3


def test_search_query_with_punctuation_does_not_blow_up(tmp_path: Path) -> None:
    conn = _ingested(tmp_path)
    hits = search_messages(conn, SearchFilters(query='pizza ()*"AND'))
    assert hits


def test_list_senders(tmp_path: Path) -> None:
    conn = _ingested(tmp_path)
    sx = list_senders(conn)
    names = {s["sender_name"] for s in sx}
    assert "Alice" in names
    assert "Carol" in names


def test_list_chats_has_message_and_photo_count(tmp_path: Path) -> None:
    conn = _ingested(tmp_path)
    chats = list_chats(conn)
    alice = next(c for c in chats if c["jid"] == "111@s.whatsapp.net")
    # Alice has 1 image + 2 text fallbacks/messages
    assert alice["photo_count"] == 1
    assert alice["message_count"] >= 2
