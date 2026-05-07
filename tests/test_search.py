from __future__ import annotations

import sqlite3
from pathlib import Path

from whatsapp_photos.ingest import ingest
from whatsapp_photos.search import (
    SearchFilters,
    count_photos,
    list_chats,
    list_senders,
    search_photos,
)

from .fixtures import build_fixture


def _ingested(tmp_path: Path) -> sqlite3.Connection:
    chat_db, media_root = build_fixture(tmp_path)
    out = tmp_path / "photos.db"
    ingest(chat_db, media_root, out)
    return sqlite3.connect(f"file:{out}?mode=ro", uri=True)


def test_search_by_caption(tmp_path: Path) -> None:
    conn = _ingested(tmp_path)
    hits = search_photos(conn, SearchFilters(query="pizza"))
    assert {h.chat_jid for h in hits} == {"111@s.whatsapp.net", "333-group@g.us"}
    # Snippet contains highlighted match
    assert any("<mark>pizza</mark>" in (h.snippet or "") for h in hits)


def test_search_by_sender(tmp_path: Path) -> None:
    conn = _ingested(tmp_path)
    hits = search_photos(conn, SearchFilters(query="Carol"))
    assert len(hits) == 1
    assert hits[0].chat_jid == "333-group@g.us"


def test_search_filter_chat(tmp_path: Path) -> None:
    conn = _ingested(tmp_path)
    chats = list_chats(conn)
    bob = next(c for c in chats if c["jid"] == "222@s.whatsapp.net")
    hits = search_photos(conn, SearchFilters(chat_id=bob["id"]))
    assert len(hits) == 1
    assert hits[0].is_from_me is True


def test_search_date_range(tmp_path: Path) -> None:
    conn = _ingested(tmp_path)
    # Only Alice's pizza (taken_at 1_700_000_000)
    hits = search_photos(
        conn,
        SearchFilters(
            since_unix=1_699_999_000,
            until_unix=1_700_005_000,
        ),
    )
    assert len(hits) == 1
    assert hits[0].chat_jid == "111@s.whatsapp.net"


def test_search_count_matches(tmp_path: Path) -> None:
    conn = _ingested(tmp_path)
    assert count_photos(conn, SearchFilters()) == 3
    assert count_photos(conn, SearchFilters(query="pizza")) == 2


def test_search_query_with_punctuation_does_not_blow_up(tmp_path: Path) -> None:
    conn = _ingested(tmp_path)
    # FTS5 syntax characters should be sanitized, not raise.
    hits = search_photos(conn, SearchFilters(query='pizza ()*"AND'))
    assert hits  # one of the meaningful tokens still matches


def test_list_senders(tmp_path: Path) -> None:
    conn = _ingested(tmp_path)
    sx = list_senders(conn)
    names = {s["sender_name"] for s in sx}
    assert "Alice" in names
    assert "Carol" in names
