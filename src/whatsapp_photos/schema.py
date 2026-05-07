"""SQLite schema for the searchable photo database.

One row per photo file. ``photos_fts`` is an FTS5 virtual table mirroring
the searchable text columns; we keep it as a *contentless* FTS index built
once after ingest, rather than synchronizing via triggers, because ingest
is a one-shot batch job.
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
    CREATE TABLE IF NOT EXISTS photos (
        id INTEGER PRIMARY KEY,
        chat_id INTEGER NOT NULL REFERENCES chats(id),
        chat_jid TEXT NOT NULL,
        chat_name TEXT,
        is_group INTEGER NOT NULL DEFAULT 0,
        sender_jid TEXT,
        sender_name TEXT,
        is_from_me INTEGER NOT NULL DEFAULT 0,
        stanza_id TEXT,
        taken_at INTEGER,                -- unix seconds (UTC)
        media_path TEXT NOT NULL,        -- as recorded in the iOS DB
        absolute_path TEXT NOT NULL,     -- resolved at ingest
        filename TEXT NOT NULL,
        file_size INTEGER,
        sha256 TEXT,
        width INTEGER,
        height INTEGER,
        mime_type TEXT,
        caption TEXT,
        UNIQUE (media_path, stanza_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS photos_taken_at ON photos (taken_at)",
    "CREATE INDEX IF NOT EXISTS photos_chat_id ON photos (chat_id)",
    "CREATE INDEX IF NOT EXISTS photos_sender ON photos (sender_jid)",
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS photos_fts USING fts5(
        caption,
        sender_name,
        chat_name,
        filename,
        content='photos',
        content_rowid='id',
        tokenize='unicode61 remove_diacritics 2'
    )
    """,
]


def init_schema(conn: sqlite3.Connection) -> None:
    for stmt in SCHEMA:
        conn.execute(stmt)
    conn.commit()


def rebuild_fts(conn: sqlite3.Connection) -> None:
    """Repopulate the FTS index from photos.

    Cheaper than maintaining triggers during a batch ingest.
    """
    conn.execute("INSERT INTO photos_fts(photos_fts) VALUES ('rebuild')")
    conn.commit()
