"""Search the WhatsApp archive database.

Combines an FTS5 query (body / sender / chat / filename) with structured
filters (date range, sender JID, chat id, group-only, message type). Used
by the CLI and the web viewer.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SearchFilters:
    query: str | None = None
    chat_id: int | None = None
    chat_jid: str | None = None
    sender_jid: str | None = None
    is_group: bool | None = None
    is_from_me: bool | None = None
    since_unix: int | None = None
    until_unix: int | None = None
    type: str | None = None  # 'text' | 'image' | None for all
    limit: int = 100
    offset: int = 0


@dataclass
class MessageHit:
    id: int
    chat_jid: str
    chat_name: str | None
    is_group: bool
    sender_jid: str | None
    sender_name: str | None
    is_from_me: bool
    sent_at: int | None
    type: str
    body: str | None
    media_path: str | None
    absolute_path: str | None
    filename: str | None
    file_size: int | None
    width: int | None
    height: int | None
    mime_type: str | None
    snippet: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


# Whitelisted ordering options to keep user-supplied input out of SQL.
_ORDER_BY = {
    "newest": "m.sent_at DESC, m.id DESC",
    "oldest": "m.sent_at ASC, m.id ASC",
    "relevance": "rank, m.sent_at DESC",
}


def search_messages(
    conn: sqlite3.Connection,
    filters: SearchFilters,
    order_by: str = "newest",
) -> list[MessageHit]:
    where: list[str] = []
    params: list[Any] = []
    sanitized_q = _sanitize_fts_query(filters.query) if filters.query else ""
    use_fts = bool(sanitized_q)

    # If the user typed something but it sanitized to nothing usable
    # (queries like '*', '()', 'AND'), don't silently fall through to
    # "no FTS" — that returns the entire archive labelled as a search
    # hit, which is more confusing than a clean empty.
    if filters.query and filters.query.strip() and not sanitized_q:
        return []

    select_cols = (
        "m.id, m.chat_jid, m.chat_name, m.is_group, m.sender_jid, m.sender_name, "
        "m.is_from_me, m.sent_at, m.type, m.body, m.media_path, m.absolute_path, "
        "m.filename, m.file_size, m.width, m.height, m.mime_type"
    )
    if use_fts:
        select_cols += ", snippet(messages_fts, 0, '<mark>', '</mark>', '…', 16) AS snippet"
        join = "FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid"
        where.append("messages_fts MATCH ?")
        params.append(sanitized_q)
    else:
        select_cols += ", NULL AS snippet"
        join = "FROM messages m"

    if filters.chat_id is not None:
        where.append("m.chat_id = ?")
        params.append(filters.chat_id)
    if filters.chat_jid is not None:
        where.append("m.chat_jid = ?")
        params.append(filters.chat_jid)
    if filters.sender_jid is not None:
        where.append("m.sender_jid = ?")
        params.append(filters.sender_jid)
    if filters.is_group is not None:
        where.append("m.is_group = ?")
        params.append(1 if filters.is_group else 0)
    if filters.is_from_me is not None:
        where.append("m.is_from_me = ?")
        params.append(1 if filters.is_from_me else 0)
    if filters.since_unix is not None:
        where.append("m.sent_at >= ?")
        params.append(filters.since_unix)
    if filters.until_unix is not None:
        where.append("m.sent_at <= ?")
        params.append(filters.until_unix)
    if filters.type is not None:
        if filters.type == "media":
            where.append("m.type IN ('image', 'audio', 'video')")
        else:
            where.append("m.type = ?")
            params.append(filters.type)

    order = _ORDER_BY.get(order_by, _ORDER_BY["newest"])
    if not use_fts and order_by == "relevance":
        order = _ORDER_BY["newest"]  # no rank without FTS

    where_clause = ("WHERE " + " AND ".join(where)) if where else ""
    sql = (
        f"SELECT {select_cols} {join} {where_clause} "
        f"ORDER BY {order} LIMIT ? OFFSET ?"
    )
    params.extend([filters.limit, filters.offset])

    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_hit(r) for r in rows]


# Back-compat alias: callers / tests that still import ``search_photos``.
search_photos = search_messages


def count_messages(conn: sqlite3.Connection, filters: SearchFilters) -> int:
    where: list[str] = []
    params: list[Any] = []
    sanitized_q = _sanitize_fts_query(filters.query) if filters.query else ""
    use_fts = bool(sanitized_q)

    # Mirror search_messages: a user-typed query that sanitizes to
    # nothing means "0 results", not "all results".
    if filters.query and filters.query.strip() and not sanitized_q:
        return 0

    if use_fts:
        join = "FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid"
        where.append("messages_fts MATCH ?")
        params.append(sanitized_q)
    else:
        join = "FROM messages m"

    if filters.chat_id is not None:
        where.append("m.chat_id = ?"); params.append(filters.chat_id)
    if filters.chat_jid is not None:
        where.append("m.chat_jid = ?"); params.append(filters.chat_jid)
    if filters.sender_jid is not None:
        where.append("m.sender_jid = ?"); params.append(filters.sender_jid)
    if filters.is_group is not None:
        where.append("m.is_group = ?"); params.append(1 if filters.is_group else 0)
    if filters.is_from_me is not None:
        where.append("m.is_from_me = ?"); params.append(1 if filters.is_from_me else 0)
    if filters.since_unix is not None:
        where.append("m.sent_at >= ?"); params.append(filters.since_unix)
    if filters.until_unix is not None:
        where.append("m.sent_at <= ?"); params.append(filters.until_unix)
    if filters.type is not None:
        if filters.type == "media":
            where.append("m.type IN ('image', 'audio', 'video')")
        else:
            where.append("m.type = ?"); params.append(filters.type)

    where_clause = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"SELECT COUNT(*) {join} {where_clause}"
    return conn.execute(sql, params).fetchone()[0]


count_photos = count_messages  # back-compat alias


def list_chats(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """One row per chat with totals and a peek at the last message.

    sender_names is read directly from the chats table (populated at
    ingest, refreshed by the startup-migration path on old DBs). The
    previous correlated GROUP_CONCAT subquery dominated landing-page
    latency at scale (~327ms of 800ms on a 665k-msg archive).
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT c.id, c.jid, c.name, c.is_group, c.sender_names, "
        "COUNT(m.id) AS message_count, "
        "SUM(CASE WHEN m.type = 'image' THEN 1 ELSE 0 END) AS photo_count, "
        "(SELECT id FROM messages WHERE chat_id = c.id AND type = 'image' "
        " ORDER BY sent_at DESC LIMIT 1) AS sample_photo_id, "
        "last_m.sent_at      AS last_message_at, "
        "last_m.body         AS last_message_body, "
        "last_m.type         AS last_message_type, "
        "last_m.is_from_me   AS last_message_from_me, "
        "last_m.sender_name  AS last_message_sender "
        "FROM chats c "
        "LEFT JOIN messages m ON m.chat_id = c.id "
        "LEFT JOIN messages last_m ON last_m.id = ("
        "  SELECT id FROM messages "
        "  WHERE chat_id = c.id AND sent_at IS NOT NULL "
        "  ORDER BY sent_at DESC, id DESC LIMIT 1"
        ") "
        "GROUP BY c.id "
        "ORDER BY last_message_at IS NULL, last_message_at DESC, "
        "         c.name COLLATE NOCASE"
    ).fetchall()
    return [dict(r) for r in rows]


def list_senders(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT sender_jid, sender_name, COUNT(*) AS message_count, "
        "SUM(CASE WHEN type = 'image' THEN 1 ELSE 0 END) AS photo_count, "
        "(SELECT id FROM messages m2 WHERE m2.sender_jid = m1.sender_jid "
        " AND m2.type = 'image' ORDER BY m2.sent_at DESC LIMIT 1) AS sample_photo_id "
        "FROM messages m1 WHERE sender_jid IS NOT NULL "
        "GROUP BY sender_jid ORDER BY message_count DESC"
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_hit(r: sqlite3.Row) -> MessageHit:
    return MessageHit(
        id=r["id"],
        chat_jid=r["chat_jid"],
        chat_name=r["chat_name"],
        is_group=bool(r["is_group"]),
        sender_jid=r["sender_jid"],
        sender_name=r["sender_name"],
        is_from_me=bool(r["is_from_me"]),
        sent_at=r["sent_at"],
        type=r["type"],
        body=r["body"],
        media_path=r["media_path"],
        absolute_path=r["absolute_path"],
        filename=r["filename"],
        file_size=r["file_size"],
        width=r["width"],
        height=r["height"],
        mime_type=r["mime_type"],
        snippet=r["snippet"] if "snippet" in r.keys() else None,
    )


_FTS_RESERVED = {"AND", "OR", "NOT", "NEAR"}


def _sanitize_fts_query(q: str) -> str:
    """Return an FTS5 MATCH expression that handles user input safely.

    SQLite FTS5 has its own mini-syntax (``AND``, ``OR``, ``NEAR``,
    column filters, quoted phrases, prefix ``*``). For an end-user search
    box we want "show me what they typed, but don't blow up on punctuation
    that FTS5 treats specially". We tokenize on whitespace and quote each
    token, allowing prefix matches via a trailing ``*`` and treating
    quoted phrases verbatim.
    """
    q = q.strip()
    if not q:
        return q
    out: list[str] = []
    i = 0
    while i < len(q):
        c = q[i]
        if c.isspace():
            i += 1
            continue
        if c == '"':
            end = q.find('"', i + 1)
            if end == -1:
                end = len(q)
            phrase = q[i + 1 : end].replace('"', "")
            out.append(f'"{phrase}"')
            i = end + 1
        else:
            j = i
            while j < len(q) and not q[j].isspace():
                j += 1
            tok = q[i:j].replace('"', "")
            tok = tok.translate(str.maketrans("", "", "()*:^"))
            if tok and tok.upper() not in _FTS_RESERVED:
                out.append(f'"{tok}"*')
            i = j
    return " ".join(out)
