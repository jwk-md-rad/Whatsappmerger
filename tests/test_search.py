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


def test_list_chats_returns_last_message_fields(tmp_path: Path) -> None:
    """The landing page needs a preview line for every chat."""
    conn = _ingested(tmp_path)
    chats = list_chats(conn)
    # The Beach Trip group has the newest message in the fixture.
    beach = next(c for c in chats if c["jid"] == "333-group@g.us")
    assert beach["last_message_at"] is not None
    assert beach["last_message_body"] is not None
    assert beach["last_message_type"] in {"text", "image"}
    # last_message_from_me is a 0/1 int from SQLite.
    assert beach["last_message_from_me"] in (0, 1)


def test_list_chats_sorted_by_last_message_desc(tmp_path: Path) -> None:
    """Newest activity must float to the top so the landing reads like
    a real messaging app."""
    conn = _ingested(tmp_path)
    chats = list_chats(conn)
    timestamps = [c["last_message_at"] for c in chats if c["last_message_at"] is not None]
    assert timestamps == sorted(timestamps, reverse=True)
    # Chats with no timestamp (none in this fixture, but guard the contract)
    # appear after the timestamped ones, never interleaved.
    saw_null = False
    for c in chats:
        if c["last_message_at"] is None:
            saw_null = True
        else:
            assert not saw_null, "null-timestamp chat appeared before a timestamped one"


def test_list_chats_sender_names_lets_us_find_group_by_member(tmp_path: Path) -> None:
    """Typing a member's name in the landing filter must surface the
    groups they're in, not just chats literally named after them."""
    conn = _ingested(tmp_path)
    chats = list_chats(conn)
    # Carol posts in 'Beach Trip' but doesn't have a 1-on-1 chat — only
    # way to find her chat from the landing filter is via sender_names.
    beach = next(c for c in chats if c["jid"] == "333-group@g.us")
    assert beach["sender_names"] is not None
    assert "Carol" in beach["sender_names"]


def test_list_chats_sender_names_excludes_self(tmp_path: Path) -> None:
    """Owner's own name must NOT be in sender_names — they're in every
    chat they own, so including "Me" would make typing their own name
    in the landing filter match every chat (= effectively no filter).
    Bob is a 1-on-1 where only "we" have posted; sender_names must be
    NULL or empty here."""
    conn = _ingested(tmp_path)
    chats = list_chats(conn)
    bob = next(c for c in chats if c["jid"] == "222@s.whatsapp.net")
    assert not (bob["sender_names"] or "").strip()
