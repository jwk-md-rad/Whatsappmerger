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


# -- iOS (Core Data) fixtures ----------------------------------------------

# A deliberately reduced ChatStorage.sqlite shape: enough Z* tables and
# columns to drive the merge, with both Z_PRIMARYKEY and Z_METADATA so the
# Core-Data-specific code paths are exercised.

IOS_SCHEMA = [
    """
    CREATE TABLE Z_PRIMARYKEY (
        Z_ENT INTEGER PRIMARY KEY,
        Z_NAME TEXT,
        Z_SUPER INTEGER,
        Z_MAX INTEGER
    )
    """,
    """
    CREATE TABLE Z_METADATA (
        Z_VERSION INTEGER PRIMARY KEY,
        Z_UUID TEXT,
        Z_PLIST BLOB
    )
    """,
    """
    CREATE TABLE ZWACHATSESSION (
        Z_PK INTEGER PRIMARY KEY AUTOINCREMENT,
        Z_ENT INTEGER,
        Z_OPT INTEGER,
        ZCONTACTJID TEXT UNIQUE,
        ZSESSIONTYPE INTEGER,
        ZPARTNERNAME TEXT,
        ZLASTMESSAGE INTEGER,
        ZLASTMESSAGEDATE REAL,
        ZMESSAGECOUNTER INTEGER
    )
    """,
    """
    CREATE TABLE ZWAMESSAGE (
        Z_PK INTEGER PRIMARY KEY AUTOINCREMENT,
        Z_ENT INTEGER,
        Z_OPT INTEGER,
        ZCHATSESSION INTEGER NOT NULL,
        ZGROUPMEMBER INTEGER,
        ZMEDIAITEM INTEGER,
        ZISFROMME INTEGER NOT NULL,
        ZSTANZAID TEXT,
        ZMESSAGEDATE REAL,
        ZMESSAGETYPE INTEGER,
        ZTEXT TEXT,
        ZFROMJID TEXT,
        ZTOJID TEXT
    )
    """,
    """
    CREATE TABLE ZWAMEDIAITEM (
        Z_PK INTEGER PRIMARY KEY AUTOINCREMENT,
        Z_ENT INTEGER,
        Z_OPT INTEGER,
        ZMESSAGE INTEGER UNIQUE,
        ZMEDIALOCALPATH TEXT,
        ZFILESIZE INTEGER
    )
    """,
    """
    CREATE TABLE ZWAGROUPMEMBER (
        Z_PK INTEGER PRIMARY KEY AUTOINCREMENT,
        Z_ENT INTEGER,
        Z_OPT INTEGER,
        ZCHATSESSION INTEGER NOT NULL,
        ZMEMBERJID TEXT NOT NULL,
        ZCONTACTNAME TEXT,
        UNIQUE (ZCHATSESSION, ZMEMBERJID)
    )
    """,
    """
    CREATE TABLE ZWAMESSAGEINFO (
        Z_PK INTEGER PRIMARY KEY AUTOINCREMENT,
        Z_ENT INTEGER,
        Z_OPT INTEGER,
        ZMESSAGE INTEGER UNIQUE,
        ZRECEIPTINFO BLOB
    )
    """,
]

# Entity-id assignments. In the "B" fixture they're deliberately different
# from "A" so the entity-id remap path is exercised.
IOS_ENTITIES_A = {
    "WAChatSession": 1,
    "WAMessage": 2,
    "WAMediaItem": 3,
    "WAGroupMember": 4,
    "WAMessageInfo": 5,
}
IOS_ENTITIES_B = {
    "WAChatSession": 11,
    "WAMessage": 12,
    "WAMediaItem": 13,
    "WAGroupMember": 14,
    "WAMessageInfo": 15,
}


def make_ios_db(
    path: Path,
    *,
    dataset: str,
    model_plist: bytes = b"<plist-A>",
) -> None:
    """Build a synthetic ChatStorage.sqlite-shaped fixture."""
    conn = sqlite3.connect(path)
    try:
        _exec_all(conn, IOS_SCHEMA)
        if dataset == "A":
            _seed_ios(conn, IOS_ENTITIES_A, _seed_ios_a, model_plist)
        elif dataset == "B":
            _seed_ios(conn, IOS_ENTITIES_B, _seed_ios_b, model_plist)
        else:
            raise ValueError(dataset)
        conn.commit()
    finally:
        conn.close()


def _seed_ios(conn, entities: dict[str, int], seed_fn, model_plist: bytes) -> None:
    for name, ent in entities.items():
        conn.execute(
            "INSERT INTO Z_PRIMARYKEY (Z_ENT, Z_NAME, Z_SUPER, Z_MAX) VALUES (?, ?, 0, 0)",
            (ent, name),
        )
    conn.execute(
        "INSERT INTO Z_METADATA (Z_VERSION, Z_UUID, Z_PLIST) VALUES (1, 'uuid-fix', ?)",
        (model_plist,),
    )
    seed_fn(conn, entities)
    # bump Z_MAX to current MAX(Z_PK) per entity (mirrors what Core Data does)
    for name in entities:
        table = "Z" + name.upper()
        actual = conn.execute(f"SELECT COALESCE(MAX(Z_PK),0) FROM {table}").fetchone()[0]
        conn.execute(
            "UPDATE Z_PRIMARYKEY SET Z_MAX = ? WHERE Z_NAME = ?", (actual, name)
        )


def _ios_add_chat(conn, ent, contact_jid, name=None) -> int:
    cur = conn.execute(
        "INSERT INTO ZWACHATSESSION (Z_ENT, Z_OPT, ZCONTACTJID, ZSESSIONTYPE, "
        "ZPARTNERNAME, ZMESSAGECOUNTER) VALUES (?, 1, ?, 0, ?, 0)",
        (ent, contact_jid, name),
    )
    return cur.lastrowid


def _ios_add_msg(
    conn,
    ent,
    chat_pk,
    is_from_me,
    stanza_id,
    text,
    *,
    date: float = 700_000_000.0,
    msg_type: int = 0,
) -> int:
    cur = conn.execute(
        "INSERT INTO ZWAMESSAGE (Z_ENT, Z_OPT, ZCHATSESSION, ZISFROMME, ZSTANZAID, "
        "ZMESSAGEDATE, ZMESSAGETYPE, ZTEXT) VALUES (?, 1, ?, ?, ?, ?, ?, ?)",
        (ent, chat_pk, is_from_me, stanza_id, date, msg_type, text),
    )
    return cur.lastrowid


def _ios_add_media(conn, ent, msg_pk, path: str, size: int) -> int:
    cur = conn.execute(
        "INSERT INTO ZWAMEDIAITEM (Z_ENT, Z_OPT, ZMESSAGE, ZMEDIALOCALPATH, ZFILESIZE) "
        "VALUES (?, 1, ?, ?, ?)",
        (ent, msg_pk, path, size),
    )
    media_pk = cur.lastrowid
    conn.execute(
        "UPDATE ZWAMESSAGE SET ZMEDIAITEM = ? WHERE Z_PK = ?", (media_pk, msg_pk)
    )
    return media_pk


def _ios_add_msginfo(conn, ent, msg_pk, blob: bytes) -> int:
    cur = conn.execute(
        "INSERT INTO ZWAMESSAGEINFO (Z_ENT, Z_OPT, ZMESSAGE, ZRECEIPTINFO) "
        "VALUES (?, 1, ?, ?)",
        (ent, msg_pk, blob),
    )
    return cur.lastrowid


def _seed_ios_a(conn, ent: dict[str, int]) -> None:
    chat_alice = _ios_add_chat(conn, ent["WAChatSession"], "111@s.whatsapp.net", "Alice")
    chat_bob = _ios_add_chat(conn, ent["WAChatSession"], "222@s.whatsapp.net", "Bob")
    m1 = _ios_add_msg(
        conn, ent["WAMessage"], chat_alice, 0, "STANZA_A1",
        "Hi from Alice (A)", date=700_000_100.0,
    )
    _ios_add_msg(
        conn, ent["WAMessage"], chat_alice, 1, "STANZA_A2",
        "Reply (A)", date=700_000_200.0,
    )
    m3 = _ios_add_msg(
        conn, ent["WAMessage"], chat_bob, 0, "STANZA_B1",
        "Bob hi (A)", date=700_000_300.0,
    )
    _ios_add_media(conn, ent["WAMediaItem"], m3, "/Library/Media/img-A.jpg", 1024)
    _ios_add_msginfo(conn, ent["WAMessageInfo"], m1, b"receipt-A1")


def _seed_ios_b(conn, ent: dict[str, int]) -> None:
    # Different Z_PK ordering, different Z_ENT ids.
    chat_bob = _ios_add_chat(conn, ent["WAChatSession"], "222@s.whatsapp.net", "Bob")
    chat_carol = _ios_add_chat(conn, ent["WAChatSession"], "333@s.whatsapp.net", "Carol")
    # STANZA_B1 overlaps with A's. STANZA_B2 is new in same chat.
    _ios_add_msg(
        conn, ent["WAMessage"], chat_bob, 0, "STANZA_B1",
        "Bob hi (B copy, ignored)", date=700_000_300.0,
    )
    m_b2 = _ios_add_msg(
        conn, ent["WAMessage"], chat_bob, 1, "STANZA_B2",
        "New from me to Bob (B)", date=700_000_400.0,
    )
    m_c1 = _ios_add_msg(
        conn, ent["WAMessage"], chat_carol, 0, "STANZA_C1",
        "From Carol (B)", date=700_000_500.0,
    )
    _ios_add_media(
        conn, ent["WAMediaItem"], m_c1, "/Library/Media/carol.jpg", 4096
    )
    _ios_add_msginfo(conn, ent["WAMessageInfo"], m_b2, b"receipt-B2")
