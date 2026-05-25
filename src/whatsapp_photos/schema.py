"""SQLite schema for the searchable WhatsApp archive database.

One row per message — text or image. ``messages_fts`` is an FTS5 virtual
table mirroring the searchable text columns; we keep it as a *contentless*
FTS index built once after ingest, rather than synchronizing via triggers,
because ingest is a one-shot batch job.

Phase B scope: text + image. Other media types (video, audio, document)
are not indexed yet and the corresponding rows are skipped at ingest
time. Adding them later only requires extending ``type``.
"""
from __future__ import annotations

import sqlite3


SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS chats (
        id INTEGER PRIMARY KEY,
        jid TEXT UNIQUE NOT NULL,
        name TEXT,
        is_group INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY,
        chat_id INTEGER NOT NULL REFERENCES chats(id),
        chat_jid TEXT NOT NULL,
        chat_name TEXT,
        is_group INTEGER NOT NULL DEFAULT 0,
        sender_jid TEXT,
        sender_name TEXT,
        is_from_me INTEGER NOT NULL DEFAULT 0,
        stanza_id TEXT,
        sent_at INTEGER,                 -- unix seconds (UTC)
        type TEXT NOT NULL,              -- 'text' | 'image'
        body TEXT,                       -- text body OR image caption
        media_path TEXT,                 -- as recorded in the iOS DB, NULL for text
        absolute_path TEXT,              -- resolved at ingest, NULL for text
        filename TEXT,                   -- NULL for text
        file_size INTEGER,
        sha256 TEXT,
        width INTEGER,
        height INTEGER,
        mime_type TEXT,
        UNIQUE (stanza_id, type, media_path)
    )
    """,
    "CREATE INDEX IF NOT EXISTS messages_sent_at ON messages (sent_at)",
    "CREATE INDEX IF NOT EXISTS messages_chat_id ON messages (chat_id)",
    "CREATE INDEX IF NOT EXISTS messages_chat_sent ON messages (chat_id, sent_at)",
    # Lets list_chats's sample_photo_id subquery (find the newest image
    # per chat) jump straight to the matching row without scanning past
    # the typically-much-more-numerous text messages.
    "CREATE INDEX IF NOT EXISTS messages_chat_image_sent "
    "ON messages (chat_id, sent_at) WHERE type = 'image'",
    "CREATE INDEX IF NOT EXISTS messages_sender ON messages (sender_jid)",
    "CREATE INDEX IF NOT EXISTS messages_type ON messages (type)",
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
        body,
        sender_name,
        chat_name,
        filename,
        content='messages',
        content_rowid='id',
        tokenize='unicode61 remove_diacritics 2'
    )
    """,
]


def init_schema(conn: sqlite3.Connection) -> None:
    for stmt in SCHEMA:
        conn.execute(stmt)
    conn.commit()


def ensure_indexes(conn: sqlite3.Connection) -> None:
    """Idempotently create just the indexes (no tables, no FTS).

    Used at server startup to upgrade older databases in place without
    requiring a re-ingest.
    """
    for stmt in SCHEMA:
        s = stmt.strip()
        if s.upper().startswith("CREATE INDEX"):
            conn.execute(stmt)
    conn.commit()


def rebuild_fts(conn: sqlite3.Connection) -> None:
    """Repopulate the FTS index from messages.

    Cheaper than maintaining triggers during a batch ingest.
    """
    conn.execute("INSERT INTO messages_fts(messages_fts) VALUES ('rebuild')")
    conn.commit()
