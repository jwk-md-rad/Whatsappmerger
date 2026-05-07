"""Synthetic dataset for trying the viewer without real WhatsApp data.

This is the "show me what it looks like" path: a self-contained set of
twelve made-up photos (colored panels with their label drawn in) across
five chats, with realistic captions, dates, senders, and a mix of 1-on-1
and group conversations. ``wa-photos demo`` builds it, ingests it, and
serves the viewer in one go — useful before you've extracted your real
backup, and as a smoke test of an install.

Nothing here is real. The chats and senders are illustrative.
"""
from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .cocoa import unix_to_cocoa


# Subset of the iOS WhatsApp Core Data schema, just enough for ingest.
_SCHEMA: list[str] = [
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


@dataclass(frozen=True)
class _DemoPhoto:
    chat_jid: str
    chat_name: str
    is_group: bool
    sender_jid: str | None
    sender_name: str | None
    is_from_me: bool
    taken_unix: int
    caption: str
    color: tuple[int, int, int]
    label: str
    filename: str


# Fictional dataset: 12 photos, 5 chats (3 1-on-1, 2 group).
_DEMO_PHOTOS: tuple[_DemoPhoto, ...] = (
    _DemoPhoto("alice@s.whatsapp.net", "Alice Chen", False,
               "alice@s.whatsapp.net", "Alice", False,
               1714003200, "best pizza in town!",
               (220, 90, 60), "Pizza Night", "pizza-night.jpg"),
    _DemoPhoto("alice@s.whatsapp.net", "Alice Chen", False,
               None, None, True,
               1714089600, "sunset from the apartment",
               (245, 130, 40), "Golden Hour", "sunset.jpg"),
    _DemoPhoto("alice@s.whatsapp.net", "Alice Chen", False,
               "alice@s.whatsapp.net", "Alice", False,
               1714435200, "new puppy!! her name is Mochi",
               (170, 140, 90), "Mochi", "puppy.jpg"),
    _DemoPhoto("bob@s.whatsapp.net", "Bob Patel", False,
               "bob@s.whatsapp.net", "Bob", False,
               1714521600, "finally got the keys",
               (40, 140, 200), "New House", "house-keys.jpg"),
    _DemoPhoto("bob@s.whatsapp.net", "Bob Patel", False,
               None, None, True,
               1714780800, "congrats! looks amazing",
               (90, 170, 80), "Reply", "reply.jpg"),
    _DemoPhoto("beach-trip@g.us", "Beach Trip 2024", True,
               "carol@s.whatsapp.net", "Carol", False,
               1715040000, "beach day! who wants to join us",
               (60, 180, 210), "Beach Day", "beach.jpg"),
    _DemoPhoto("beach-trip@g.us", "Beach Trip 2024", True,
               "dan@s.whatsapp.net", "Dan", False,
               1715126400, "pizza on the boardwalk",
               (210, 100, 80), "Boardwalk", "boardwalk.jpg"),
    _DemoPhoto("beach-trip@g.us", "Beach Trip 2024", True,
               None, None, True,
               1715212800, "group selfie!!",
               (180, 90, 160), "Group Selfie", "selfie.jpg"),
    _DemoPhoto("family@g.us", "Family Group", True,
               "mom@s.whatsapp.net", "Mom", False,
               1715472000, "birthday cake for grandpa",
               (230, 110, 140), "Birthday", "cake.jpg"),
    _DemoPhoto("family@g.us", "Family Group", True,
               "dad@s.whatsapp.net", "Dad", False,
               1715558400, "flowers from the garden",
               (130, 180, 90), "Garden", "flowers.jpg"),
    _DemoPhoto("eve@s.whatsapp.net", "Eve Rodriguez", False,
               "eve@s.whatsapp.net", "Eve", False,
               1715817600, "hiking trail near the lake",
               (90, 150, 100), "Hike", "hike.jpg"),
    _DemoPhoto("eve@s.whatsapp.net", "Eve Rodriguez", False,
               None, None, True,
               1715904000, "recipe i was telling you about",
               (200, 150, 60), "Recipe", "recipe.jpg"),
)


PHOTO_COUNT = len(_DEMO_PHOTOS)


# Cross-OS candidate fonts for the label baked into each demo photo.
_LABEL_FONTS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial Bold.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
)


def build_demo_fixture(root: Path) -> tuple[Path, Path]:
    """Create ``ChatStorage.sqlite`` + ``Message/Media`` tree under ``root``.

    Returns ``(chat_db, media_root)`` ready to feed to ``ingest()``.

    Idempotent: any existing contents under ``root`` are wiped first, so
    re-running the demo command always reflects the latest fixture.
    """
    root = Path(root)
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    media_root = root / "extract"
    chat_db = root / "ChatStorage.sqlite"

    for photo in _DEMO_PHOTOS:
        rel = f"Message/Media/{photo.chat_jid}/{photo.filename}"
        _draw_panel(media_root / rel, photo.color, photo.label)

    conn = sqlite3.connect(chat_db)
    try:
        for stmt in _SCHEMA:
            conn.execute(stmt)
        _populate(conn)
        conn.commit()
    finally:
        conn.close()

    return chat_db, media_root


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _populate(conn: sqlite3.Connection) -> None:
    chats: dict[str, int] = {}
    for p in _DEMO_PHOTOS:
        if p.chat_jid in chats:
            continue
        cur = conn.execute(
            "INSERT INTO ZWACHATSESSION (Z_ENT, Z_OPT, ZCONTACTJID, ZSESSIONTYPE, ZPARTNERNAME) "
            "VALUES (1, 1, ?, ?, ?)",
            (p.chat_jid, 1 if p.is_group else 0, p.chat_name),
        )
        chats[p.chat_jid] = cur.lastrowid

    members: dict[tuple[str, str], int] = {}
    for p in _DEMO_PHOTOS:
        if not p.is_group or not p.sender_jid:
            continue
        key = (p.chat_jid, p.sender_jid)
        if key in members:
            continue
        cur = conn.execute(
            "INSERT INTO ZWAGROUPMEMBER (Z_ENT, Z_OPT, ZCHATSESSION, ZMEMBERJID, ZCONTACTNAME) "
            "VALUES (4, 1, ?, ?, ?)",
            (chats[p.chat_jid], p.sender_jid, p.sender_name),
        )
        members[key] = cur.lastrowid

    for i, p in enumerate(_DEMO_PHOTOS):
        gm = members.get((p.chat_jid, p.sender_jid)) if p.is_group and p.sender_jid else None
        cur = conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_ENT, Z_OPT, ZCHATSESSION, ZGROUPMEMBER, ZISFROMME, "
            "ZSTANZAID, ZMESSAGEDATE, ZMESSAGETYPE, ZTEXT, ZFROMJID, ZPUSHNAME) "
            "VALUES (2, 1, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
            (
                chats[p.chat_jid],
                gm,
                int(p.is_from_me),
                f"DEMO{i:03d}",
                unix_to_cocoa(p.taken_unix),
                p.caption,
                p.sender_jid,
                p.sender_name,
            ),
        )
        msg_pk = cur.lastrowid
        rel = f"Message/Media/{p.chat_jid}/{p.filename}"
        cur = conn.execute(
            "INSERT INTO ZWAMEDIAITEM (Z_ENT, Z_OPT, ZMESSAGE, ZMEDIALOCALPATH, ZFILESIZE) "
            "VALUES (3, 1, ?, ?, NULL)",
            (msg_pk, rel),
        )
        conn.execute(
            "UPDATE ZWAMESSAGE SET ZMEDIAITEM = ? WHERE Z_PK = ?",
            (cur.lastrowid, msg_pk),
        )


def _draw_panel(
    path: Path,
    color: tuple[int, int, int],
    label: str,
    size: tuple[int, int] = (720, 540),
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", size, color)
    draw = ImageDraw.Draw(image)
    font = _load_label_font()
    bbox = draw.textbbox((0, 0), label, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    draw.text(
        ((size[0] - text_w) / 2, (size[1] - text_h) / 2 - 20),
        label,
        fill=(255, 255, 255),
        font=font,
    )
    image.save(path, "JPEG", quality=85)


def _load_label_font() -> ImageFont.ImageFont:
    for candidate in _LABEL_FONTS:
        if Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, 64)
            except OSError:
                continue
    return ImageFont.load_default()
