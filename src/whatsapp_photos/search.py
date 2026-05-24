"""Search the photo database.

Combines an FTS5 query (caption / sender / chat / filename) with structured
filters (date range, sender JID, chat id, group-only). Used by the CLI and
the web viewer.
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
    limit: int = 100
    offset: int = 0


@dataclass
class PhotoHit:
    id: int
    chat_jid: str
    chat_name: str | None
    is_group: bool
    sender_jid: str | None
    sender_name: str | None
    is_from_me: bool
    taken_at: int | None
    media_path: str
    absolute_path: str
    filename: str
    file_size: int | None
    width: int | None
    height: int | None
    mime_type: str | None
    caption: str | None
    snippet: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


# Whitelisted ordering options to keep user-supplied input out of SQL.
_ORDER_BY = {
    "newest": "p.taken_at DESC, p.id DESC",
    "oldest": "p.taken_at ASC, p.id ASC",
    "relevance": "rank, p.taken_at DESC",
}


def search_photos(
    conn: sqlite3.Connection,
    filters: SearchFilters,
    order_by: str = "newest",
) -> list[PhotoHit]:
    where: list[str] = []
    params: list[Any] = []
    use_fts = bool(filters.query and filters.query.strip())

    select_cols = (
        "p.id, p.chat_jid, p.chat_name, p.is_group, p.sender_jid, p.sender_name, "
        "p.is_from_me, p.taken_at, p.media_path, p.absolute_path, p.filename, "
        "p.file_size, p.width, p.height, p.mime_type, p.caption"
    )
    if use_fts:
        select_cols += ", snippet(photos_fts, 0, '<mark>', '</mark>', '…', 12) AS snippet"
        join = "FROM photos_fts JOIN photos p ON p.id = photos_fts.rowid"
        where.append("photos_fts MATCH ?")
        params.append(_sanitize_fts_query(filters.query))
    else:
        select_cols += ", NULL AS snippet"
        join = "FROM photos p"

    if filters.chat_id is not None:
        where.append("p.chat_id = ?")
        params.append(filters.chat_id)
    if filters.chat_jid is not None:
        where.append("p.chat_jid = ?")
        params.append(filters.chat_jid)
    if filters.sender_jid is not None:
        where.append("p.sender_jid = ?")
        params.append(filters.sender_jid)
    if filters.is_group is not None:
        where.append("p.is_group = ?")
        params.append(1 if filters.is_group else 0)
    if filters.is_from_me is not None:
        where.append("p.is_from_me = ?")
        params.append(1 if filters.is_from_me else 0)
    if filters.since_unix is not None:
        where.append("p.taken_at >= ?")
        params.append(filters.since_unix)
    if filters.until_unix is not None:
        where.append("p.taken_at <= ?")
        params.append(filters.until_unix)

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


def count_photos(conn: sqlite3.Connection, filters: SearchFilters) -> int:
    where: list[str] = []
    params: list[Any] = []
    use_fts = bool(filters.query and filters.query.strip())
    if use_fts:
        join = "FROM photos_fts JOIN photos p ON p.id = photos_fts.rowid"
        where.append("photos_fts MATCH ?")
        params.append(_sanitize_fts_query(filters.query))
    else:
        join = "FROM photos p"

    if filters.chat_id is not None:
        where.append("p.chat_id = ?"); params.append(filters.chat_id)
    if filters.chat_jid is not None:
        where.append("p.chat_jid = ?"); params.append(filters.chat_jid)
    if filters.sender_jid is not None:
        where.append("p.sender_jid = ?"); params.append(filters.sender_jid)
    if filters.is_group is not None:
        where.append("p.is_group = ?"); params.append(1 if filters.is_group else 0)
    if filters.is_from_me is not None:
        where.append("p.is_from_me = ?"); params.append(1 if filters.is_from_me else 0)
    if filters.since_unix is not None:
        where.append("p.taken_at >= ?"); params.append(filters.since_unix)
    if filters.until_unix is not None:
        where.append("p.taken_at <= ?"); params.append(filters.until_unix)

    where_clause = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"SELECT COUNT(*) {join} {where_clause}"
    return conn.execute(sql, params).fetchone()[0]


def list_chats(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT c.id, c.jid, c.name, c.is_group, COUNT(p.id) AS photo_count "
        "FROM chats c LEFT JOIN photos p ON p.chat_id = c.id "
        "GROUP BY c.id ORDER BY photo_count DESC, c.name"
    ).fetchall()
    return [dict(r) for r in rows]


def list_senders(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT sender_jid, sender_name, COUNT(*) AS photo_count FROM photos "
        "WHERE sender_jid IS NOT NULL "
        "GROUP BY sender_jid ORDER BY photo_count DESC"
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_hit(r: sqlite3.Row) -> PhotoHit:
    return PhotoHit(
        id=r["id"],
        chat_jid=r["chat_jid"],
        chat_name=r["chat_name"],
        is_group=bool(r["is_group"]),
        sender_jid=r["sender_jid"],
        sender_name=r["sender_name"],
        is_from_me=bool(r["is_from_me"]),
        taken_at=r["taken_at"],
        media_path=r["media_path"],
        absolute_path=r["absolute_path"],
        filename=r["filename"],
        file_size=r["file_size"],
        width=r["width"],
        height=r["height"],
        mime_type=r["mime_type"],
        caption=r["caption"],
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
            # Strip FTS-meta characters from the bare token.
            tok = tok.translate(str.maketrans("", "", "()*:^"))
            if tok and tok.upper() not in _FTS_RESERVED:
                out.append(f'"{tok}"*')
            i = j
    return " ".join(out)
