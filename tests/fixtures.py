"""Build a synthetic iOS-WhatsApp-shaped store and Media tree for tests."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from PIL import Image

from whatsapp_photos.cocoa import unix_to_cocoa


IOS_SCHEMA = [
    """
    CREATE TABLE Z_PRIMARYKEY (
        Z_ENT INTEGER PRIMARY KEY, Z_NAME TEXT, Z_SUPER INTEGER, Z_MAX INTEGER
    )
    """,
    """
    CREATE TABLE Z_METADATA (
        Z_VERSION INTEGER PRIMARY KEY, Z_UUID TEXT, Z_PLIST BLOB
    )
    """,
    """
    CREATE TABLE ZWACHATSESSION (
        Z_PK INTEGER PRIMARY KEY AUTOINCREMENT,
        Z_ENT INTEGER, Z_OPT INTEGER,
        ZCONTACTJID TEXT UNIQUE,
        ZSESSIONTYPE INTEGER,
        ZPARTNERNAME TEXT
    )
    """,
    """
    CREATE TABLE ZWAMESSAGE (
        Z_PK INTEGER PRIMARY KEY AUTOINCREMENT,
        Z_ENT INTEGER, Z_OPT INTEGER,
        ZCHATSESSION INTEGER NOT NULL,
        ZGROUPMEMBER INTEGER,
        ZMEDIAITEM INTEGER,
        ZISFROMME INTEGER NOT NULL,
        ZSTANZAID TEXT,
        ZMESSAGEDATE REAL,
        ZMESSAGETYPE INTEGER,
        ZTEXT TEXT,
        ZFROMJID TEXT,
        ZPUSHNAME TEXT
    )
    """,
    """
    CREATE TABLE ZWAMEDIAITEM (
        Z_PK INTEGER PRIMARY KEY AUTOINCREMENT,
        Z_ENT INTEGER, Z_OPT INTEGER,
        ZMESSAGE INTEGER UNIQUE,
        ZMEDIALOCALPATH TEXT,
        ZFILESIZE INTEGER
    )
    """,
    """
    CREATE TABLE ZWAGROUPMEMBER (
        Z_PK INTEGER PRIMARY KEY AUTOINCREMENT,
        Z_ENT INTEGER, Z_OPT INTEGER,
        ZCHATSESSION INTEGER NOT NULL,
        ZMEMBERJID TEXT NOT NULL,
        ZCONTACTNAME TEXT
    )
    """,
]


def _exec_all(c, stmts):
    for s in stmts:
        c.execute(s)


def make_png(path: Path, color: tuple[int, int, int] = (200, 50, 50), size=(40, 30)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, "PNG")


def make_text_file(path: Path, content: str = "not an image"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def build_fixture(tmp_path: Path) -> tuple[Path, Path]:
    """Create a ChatStorage.sqlite + Media tree under ``tmp_path``.

    Returns ``(chat_db, media_root)``. The media root is the directory that,
    when joined with the relative ``ZMEDIALOCALPATH`` strings in the DB,
    finds the actual files. We mimic the typical iMazing-extracted layout:
    ``Message/Media/<chat>/<filename>``.
    """
    chat_db = tmp_path / "ChatStorage.sqlite"
    media_root = tmp_path / "AppDomainGroup-group.net.whatsapp.WhatsApp.shared"
    msg_media = media_root / "Message" / "Media"

    # On-disk media files
    make_png(msg_media / "111@s.whatsapp.net" / "alice-pizza.jpg", (220, 120, 60))
    make_png(msg_media / "222@s.whatsapp.net" / "bob-cat.jpg", (120, 120, 220))
    make_png(msg_media / "333-group@g.us" / "carol-beach.jpg", (60, 200, 220))
    # A row that points at a missing file
    # (no file at zzz/missing.jpg; row will be skipped)
    # A row that points at a non-image (also skipped)
    make_text_file(msg_media / "444@s.whatsapp.net" / "note.txt")

    conn = sqlite3.connect(chat_db)
    try:
        _exec_all(conn, IOS_SCHEMA)
        # Chats
        chat_alice = _add_chat(conn, "111@s.whatsapp.net", "Alice", session_type=0)
        chat_bob = _add_chat(conn, "222@s.whatsapp.net", "Bob", session_type=0)
        chat_group = _add_chat(conn, "333-group@g.us", "Beach Trip", session_type=1)
        chat_dave = _add_chat(conn, "444@s.whatsapp.net", "Dave", session_type=0)

        # Group member: Carol in the group chat
        carol_pk = _add_group_member(
            conn, chat_group, "555@s.whatsapp.net", "Carol"
        )

        # Messages with media
        _add_msg_with_media(
            conn,
            chat_pk=chat_alice,
            stanza="STANZA_A1",
            is_from_me=0,
            from_jid="111@s.whatsapp.net",
            push_name="Alice",
            text="best pizza in town",
            taken_unix=1_700_000_000,
            rel_path="Message/Media/111@s.whatsapp.net/alice-pizza.jpg",
        )
        _add_msg_with_media(
            conn,
            chat_pk=chat_bob,
            stanza="STANZA_B1",
            is_from_me=1,
            from_jid=None,
            push_name=None,
            text="my cat eating",
            taken_unix=1_700_010_000,
            rel_path="Message/Media/222@s.whatsapp.net/bob-cat.jpg",
        )
        _add_msg_with_media(
            conn,
            chat_pk=chat_group,
            stanza="STANZA_G1",
            is_from_me=0,
            from_jid="555@s.whatsapp.net",
            push_name="Carol",
            text="beach day! pizza on the sand",
            taken_unix=1_700_020_000,
            rel_path="Message/Media/333-group@g.us/carol-beach.jpg",
            group_member_pk=carol_pk,
        )
        # Missing-file row
        _add_msg_with_media(
            conn,
            chat_pk=chat_alice,
            stanza="STANZA_A2",
            is_from_me=0,
            from_jid="111@s.whatsapp.net",
            push_name="Alice",
            text="vanished photo",
            taken_unix=1_700_030_000,
            rel_path="Message/Media/zzz/missing.jpg",
        )
        # Non-image media: file exists but isn't an image (will fall back
        # to the text body, so this row becomes a text message).
        _add_msg_with_media(
            conn,
            chat_pk=chat_dave,
            stanza="STANZA_D1",
            is_from_me=0,
            from_jid="444@s.whatsapp.net",
            push_name="Dave",
            text="check this file",
            taken_unix=1_700_040_000,
            rel_path="Message/Media/444@s.whatsapp.net/note.txt",
        )
        # Pure-text messages (no media at all).
        _add_msg_text(
            conn,
            chat_pk=chat_alice,
            stanza="STANZA_T_A1",
            is_from_me=1,
            from_jid=None,
            push_name=None,
            text="see you at the restaurant tonight",
            taken_unix=1_700_050_000,
        )
        _add_msg_text(
            conn,
            chat_pk=chat_group,
            stanza="STANZA_T_G1",
            is_from_me=0,
            from_jid="555@s.whatsapp.net",
            push_name="Carol",
            text="anyone bringing sunscreen?",
            taken_unix=1_700_060_000,
            group_member_pk=carol_pk,
        )

        conn.commit()
    finally:
        conn.close()

    return chat_db, media_root


def _add_chat(conn, jid: str, name: str, session_type: int) -> int:
    cur = conn.execute(
        "INSERT INTO ZWACHATSESSION (Z_ENT, Z_OPT, ZCONTACTJID, ZSESSIONTYPE, ZPARTNERNAME) "
        "VALUES (1, 1, ?, ?, ?)",
        (jid, session_type, name),
    )
    return cur.lastrowid


def _add_group_member(conn, chat_pk: int, jid: str, name: str) -> int:
    cur = conn.execute(
        "INSERT INTO ZWAGROUPMEMBER (Z_ENT, Z_OPT, ZCHATSESSION, ZMEMBERJID, ZCONTACTNAME) "
        "VALUES (4, 1, ?, ?, ?)",
        (chat_pk, jid, name),
    )
    return cur.lastrowid


def _add_msg_text(
    conn,
    *,
    chat_pk: int,
    stanza: str,
    is_from_me: int,
    from_jid: str | None,
    push_name: str | None,
    text: str,
    taken_unix: int,
    group_member_pk: int | None = None,
) -> None:
    cocoa = unix_to_cocoa(taken_unix)
    conn.execute(
        "INSERT INTO ZWAMESSAGE (Z_ENT, Z_OPT, ZCHATSESSION, ZGROUPMEMBER, "
        "ZISFROMME, ZSTANZAID, ZMESSAGEDATE, ZMESSAGETYPE, ZTEXT, ZFROMJID, ZPUSHNAME) "
        "VALUES (2, 1, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
        (chat_pk, group_member_pk, is_from_me, stanza, cocoa, text, from_jid, push_name),
    )


def _add_msg_with_media(
    conn,
    *,
    chat_pk: int,
    stanza: str,
    is_from_me: int,
    from_jid: str | None,
    push_name: str | None,
    text: str | None,
    taken_unix: int,
    rel_path: str,
    group_member_pk: int | None = None,
) -> None:
    cocoa = unix_to_cocoa(taken_unix)
    cur = conn.execute(
        "INSERT INTO ZWAMESSAGE (Z_ENT, Z_OPT, ZCHATSESSION, ZGROUPMEMBER, "
        "ZISFROMME, ZSTANZAID, ZMESSAGEDATE, ZMESSAGETYPE, ZTEXT, ZFROMJID, ZPUSHNAME) "
        "VALUES (2, 1, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
        (chat_pk, group_member_pk, is_from_me, stanza, cocoa, text, from_jid, push_name),
    )
    msg_pk = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO ZWAMEDIAITEM (Z_ENT, Z_OPT, ZMESSAGE, ZMEDIALOCALPATH, ZFILESIZE) "
        "VALUES (3, 1, ?, ?, NULL)",
        (msg_pk, rel_path),
    )
    conn.execute(
        "UPDATE ZWAMESSAGE SET ZMEDIAITEM = ? WHERE Z_PK = ?",
        (cur.lastrowid, msg_pk),
    )
