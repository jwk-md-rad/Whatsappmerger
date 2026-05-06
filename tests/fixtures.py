"""Build synthetic msgstore.db files for testing.

We model both schema flavors with a deliberately reduced column set: enough
to exercise dedup, ID remapping, and satellite tables, but small enough to
read at a glance.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


MODERN_SCHEMA = [
    """
    CREATE TABLE jid (
        _id INTEGER PRIMARY KEY AUTOINCREMENT,
        user TEXT NOT NULL,
        server TEXT NOT NULL,
        agent INTEGER,
        device INTEGER,
        type INTEGER,
        raw_string TEXT UNIQUE
    )
    """,
    """
    CREATE TABLE chat (
        _id INTEGER PRIMARY KEY AUTOINCREMENT,
        jid_row_id INTEGER UNIQUE,
        subject TEXT,
        created_timestamp INTEGER,
        sort_timestamp INTEGER
    )
    """,
    """
    CREATE TABLE message (
        _id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_row_id INTEGER NOT NULL,
        from_me INTEGER NOT NULL,
        key_id TEXT NOT NULL,
        sender_jid_row_id INTEGER,
        timestamp INTEGER,
        received_timestamp INTEGER,
        message_type INTEGER,
        text_data TEXT,
        quoted_row_id INTEGER,
        status INTEGER
    )
    """,
    """
    CREATE UNIQUE INDEX message_key_unique
        ON message (chat_row_id, from_me, key_id, sender_jid_row_id)
    """,
    """
    CREATE TABLE message_media (
        message_row_id INTEGER PRIMARY KEY,
        chat_row_id INTEGER,
        file_path TEXT,
        file_size INTEGER,
        mime_type TEXT
    )
    """,
    """
    CREATE TABLE message_thumbnail (
        message_row_id INTEGER PRIMARY KEY,
        thumbnail BLOB
    )
    """,
    """
    CREATE TABLE group_participant_user (
        _id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_jid_row_id INTEGER NOT NULL,
        user_jid_row_id INTEGER NOT NULL,
        rank INTEGER,
        UNIQUE (group_jid_row_id, user_jid_row_id)
    )
    """,
]


LEGACY_SCHEMA = [
    """
    CREATE TABLE messages (
        _id INTEGER PRIMARY KEY AUTOINCREMENT,
        key_remote_jid TEXT NOT NULL,
        key_from_me INTEGER,
        key_id TEXT NOT NULL,
        status INTEGER,
        data TEXT,
        timestamp INTEGER,
        received_timestamp INTEGER,
        media_wa_type TEXT,
        UNIQUE (key_remote_jid, key_from_me, key_id)
    )
    """,
]


def _exec_all(conn: sqlite3.Connection, statements: list[str]) -> None:
    for stmt in statements:
        conn.execute(stmt)


def make_modern_db(path: Path, *, dataset: str) -> None:
    """Create a modern-schema fixture. ``dataset`` selects message content."""
    conn = sqlite3.connect(path)
    try:
        _exec_all(conn, MODERN_SCHEMA)
        if dataset == "A":
            _seed_modern_a(conn)
        elif dataset == "B":
            _seed_modern_b(conn)
        elif dataset == "overlap":
            _seed_modern_overlap(conn)
        else:
            raise ValueError(dataset)
        conn.commit()
    finally:
        conn.close()


def make_legacy_db(path: Path, *, dataset: str) -> None:
    conn = sqlite3.connect(path)
    try:
        _exec_all(conn, LEGACY_SCHEMA)
        if dataset == "A":
            _seed_legacy_a(conn)
        elif dataset == "B":
            _seed_legacy_b(conn)
        else:
            raise ValueError(dataset)
        conn.commit()
    finally:
        conn.close()


# -- Modern fixtures --------------------------------------------------------


def _add_jid(conn: sqlite3.Connection, raw: str) -> int:
    user, _, server = raw.partition("@")
    cur = conn.execute(
        "INSERT INTO jid (user, server, raw_string) VALUES (?, ?, ?)",
        (user, server, raw),
    )
    return cur.lastrowid


def _add_chat(conn: sqlite3.Connection, jid_row_id: int, subject: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO chat (jid_row_id, subject) VALUES (?, ?)",
        (jid_row_id, subject),
    )
    return cur.lastrowid


def _add_msg(
    conn: sqlite3.Connection,
    chat_row_id: int,
    from_me: int,
    key_id: str,
    sender_jid_row_id: int | None,
    text: str,
    *,
    ts: int = 1_700_000_000,
    rts: int | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO message (chat_row_id, from_me, key_id, sender_jid_row_id, "
        "timestamp, received_timestamp, message_type, text_data) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
        (chat_row_id, from_me, key_id, sender_jid_row_id, ts, rts or ts, text),
    )
    return cur.lastrowid


def _seed_modern_a(conn: sqlite3.Connection) -> None:
    alice = _add_jid(conn, "111@s.whatsapp.net")
    bob = _add_jid(conn, "222@s.whatsapp.net")
    me = _add_jid(conn, "999@s.whatsapp.net")
    chat_a = _add_chat(conn, alice, subject=None)
    chat_b = _add_chat(conn, bob, subject=None)
    _add_msg(conn, chat_a, 0, "KEY_A1", alice, "Hello from Alice (A)")
    _add_msg(conn, chat_a, 1, "KEY_A2", me, "Reply to Alice (A)")
    _add_msg(conn, chat_b, 0, "KEY_B1", bob, "Hi (A)")
    # message_media on KEY_B1
    msg_media_id = conn.execute(
        "SELECT _id FROM message WHERE key_id='KEY_B1'"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO message_media (message_row_id, chat_row_id, file_path, mime_type) "
        "VALUES (?, ?, ?, ?)",
        (msg_media_id, chat_b, "/Media/img-A.jpg", "image/jpeg"),
    )


def _seed_modern_b(conn: sqlite3.Connection) -> None:
    # Different autoincrement ordering on purpose
    bob = _add_jid(conn, "222@s.whatsapp.net")
    carol = _add_jid(conn, "333@s.whatsapp.net")
    me = _add_jid(conn, "999@s.whatsapp.net")
    chat_b = _add_chat(conn, bob)
    chat_c = _add_chat(conn, carol)
    # KEY_B1 overlaps with A's; KEY_B2 is new
    _add_msg(conn, chat_b, 0, "KEY_B1", bob, "Hi (B copy, ignored)")
    m_b2 = _add_msg(conn, chat_b, 1, "KEY_B2", me, "New from me to Bob")
    m_c1 = _add_msg(conn, chat_c, 0, "KEY_C1", carol, "From Carol")
    # quote: m_c1 quotes m_b2
    conn.execute("UPDATE message SET quoted_row_id=? WHERE _id=?", (m_b2, m_c1))


def _seed_modern_overlap(conn: sqlite3.Connection) -> None:
    """Same JIDs/chats but a message with a newer received_timestamp."""
    alice = _add_jid(conn, "111@s.whatsapp.net")
    me = _add_jid(conn, "999@s.whatsapp.net")
    chat_a = _add_chat(conn, alice)
    # Same key as A's KEY_A1 but with a fresher timestamp
    _add_msg(
        conn, chat_a, 0, "KEY_A1", alice,
        "Hello from Alice (newer)",
        ts=1_700_000_500, rts=1_700_000_500,
    )


# -- Legacy fixtures --------------------------------------------------------


def _add_legacy_msg(conn, jid, from_me, key_id, text, ts=1_600_000_000):
    conn.execute(
        "INSERT INTO messages (key_remote_jid, key_from_me, key_id, status, data, timestamp, received_timestamp) "
        "VALUES (?, ?, ?, 0, ?, ?, ?)",
        (jid, from_me, key_id, text, ts, ts),
    )


def _seed_legacy_a(conn: sqlite3.Connection) -> None:
    _add_legacy_msg(conn, "111@s.whatsapp.net", 0, "L1", "legacy A1")
    _add_legacy_msg(conn, "111@s.whatsapp.net", 1, "L2", "legacy A2")


def _seed_legacy_b(conn: sqlite3.Connection) -> None:
    _add_legacy_msg(conn, "111@s.whatsapp.net", 0, "L1", "duplicate, ignored")
    _add_legacy_msg(conn, "222@s.whatsapp.net", 0, "L3", "legacy B3")
