"""Synthetic dataset for trying the viewer without real WhatsApp data.

This is the "show me what it looks like" path: a self-contained set of
photos, text messages, and voice notes across five chats, with
realistic captions, dates, senders, and a mix of 1-on-1 and group
conversations. ``wa-photos demo`` builds it, ingests it, and serves
the viewer in one go — useful before you've extracted your real
backup, and as a smoke test of an install.

Nothing here is real. The chats and senders are illustrative.
"""
from __future__ import annotations

import math
import shutil
import sqlite3
import wave
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


@dataclass(frozen=True)
class _DemoText:
    chat_jid: str
    chat_name: str
    is_group: bool
    sender_jid: str | None
    sender_name: str | None
    is_from_me: bool
    taken_unix: int
    body: str


@dataclass(frozen=True)
class _DemoAudio:
    chat_jid: str
    chat_name: str
    is_group: bool
    sender_jid: str | None
    sender_name: str | None
    is_from_me: bool
    taken_unix: int
    filename: str
    tone_hz: int = 440
    duration_s: float = 1.5


_DEMO_AUDIOS: tuple[_DemoAudio, ...] = (
    _DemoAudio("alice@s.whatsapp.net", "Alice Chen", False,
               "alice@s.whatsapp.net", "Alice", False,
               1714003150, "voice-alice-1.wav", tone_hz=440, duration_s=2.0),
    _DemoAudio("beach-trip@g.us", "Beach Trip 2024", True,
               "dan@s.whatsapp.net", "Dan", False,
               1715040250, "voice-dan-beach.wav", tone_hz=523, duration_s=1.5),
    _DemoAudio("eve@s.whatsapp.net", "Eve Rodriguez", False,
               None, None, True,
               1715817550, "voice-me-hike.wav", tone_hz=349, duration_s=1.2),
)


_DEMO_TEXTS: tuple[_DemoText, ...] = (
    _DemoText("alice@s.whatsapp.net", "Alice Chen", False,
              "alice@s.whatsapp.net", "Alice", False,
              1714003000, "are you free for dinner tonight?"),
    _DemoText("alice@s.whatsapp.net", "Alice Chen", False,
              None, None, True,
              1714003100, "yes, the pizza place around the corner works"),
    _DemoText("bob@s.whatsapp.net", "Bob Patel", False,
              "bob@s.whatsapp.net", "Bob", False,
              1714521500, "we got the house!! moving in next month"),
    _DemoText("beach-trip@g.us", "Beach Trip 2024", True,
              "carol@s.whatsapp.net", "Carol", False,
              1715040100, "who's bringing sunscreen and towels?"),
    _DemoText("beach-trip@g.us", "Beach Trip 2024", True,
              "dan@s.whatsapp.net", "Dan", False,
              1715040200, "i'll bring the cooler and drinks"),
    _DemoText("family@g.us", "Family Group", True,
              "mom@s.whatsapp.net", "Mom", False,
              1715471900, "grandpa's party starts at 4pm don't be late"),
    _DemoText("eve@s.whatsapp.net", "Eve Rodriguez", False,
              None, None, True,
              1715817500, "thanks for the recommendation, the hike was great"),
    _DemoText("eve@s.whatsapp.net", "Eve Rodriguez", False,
              "eve@s.whatsapp.net", "Eve", False,
              1715903900, "here's that recipe i mentioned"),
)


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
    for audio in _DEMO_AUDIOS:
        rel = f"Message/Media/{audio.chat_jid}/{audio.filename}"
        _write_wav(media_root / rel, audio.tone_hz, audio.duration_s)

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
    all_items = list(_DEMO_PHOTOS) + list(_DEMO_TEXTS) + list(_DEMO_AUDIOS)
    for it in all_items:
        if it.chat_jid in chats:
            continue
        cur = conn.execute(
            "INSERT INTO ZWACHATSESSION (Z_ENT, Z_OPT, ZCONTACTJID, ZSESSIONTYPE, ZPARTNERNAME) "
            "VALUES (1, 1, ?, ?, ?)",
            (it.chat_jid, 1 if it.is_group else 0, it.chat_name),
        )
        chats[it.chat_jid] = cur.lastrowid

    members: dict[tuple[str, str], int] = {}
    for it in all_items:
        if not it.is_group or not it.sender_jid:
            continue
        key = (it.chat_jid, it.sender_jid)
        if key in members:
            continue
        cur = conn.execute(
            "INSERT INTO ZWAGROUPMEMBER (Z_ENT, Z_OPT, ZCHATSESSION, ZMEMBERJID, ZCONTACTNAME) "
            "VALUES (4, 1, ?, ?, ?)",
            (chats[it.chat_jid], it.sender_jid, it.sender_name),
        )
        members[key] = cur.lastrowid

    # Photo messages (with attached media).
    for i, p in enumerate(_DEMO_PHOTOS):
        gm = members.get((p.chat_jid, p.sender_jid)) if p.is_group and p.sender_jid else None
        cur = conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_ENT, Z_OPT, ZCHATSESSION, ZGROUPMEMBER, ZISFROMME, "
            "ZSTANZAID, ZMESSAGEDATE, ZMESSAGETYPE, ZTEXT, ZFROMJID, ZPUSHNAME) "
            "VALUES (2, 1, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
            (
                chats[p.chat_jid], gm, int(p.is_from_me),
                f"DEMO_P{i:03d}", unix_to_cocoa(p.taken_unix),
                p.caption, p.sender_jid, p.sender_name,
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

    # Text-only messages (no media attached).
    for i, t in enumerate(_DEMO_TEXTS):
        gm = members.get((t.chat_jid, t.sender_jid)) if t.is_group and t.sender_jid else None
        conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_ENT, Z_OPT, ZCHATSESSION, ZGROUPMEMBER, ZISFROMME, "
            "ZSTANZAID, ZMESSAGEDATE, ZMESSAGETYPE, ZTEXT, ZFROMJID, ZPUSHNAME) "
            "VALUES (2, 1, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
            (
                chats[t.chat_jid], gm, int(t.is_from_me),
                f"DEMO_T{i:03d}", unix_to_cocoa(t.taken_unix),
                t.body, t.sender_jid, t.sender_name,
            ),
        )

    # Audio messages (voice notes). Same WhatsApp message-type as image
    # (ZMESSAGETYPE=1 means "has media"); the ingester classifies by file
    # extension. ZTEXT is NULL because demo voice notes have no caption.
    for i, a in enumerate(_DEMO_AUDIOS):
        gm = members.get((a.chat_jid, a.sender_jid)) if a.is_group and a.sender_jid else None
        cur = conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_ENT, Z_OPT, ZCHATSESSION, ZGROUPMEMBER, ZISFROMME, "
            "ZSTANZAID, ZMESSAGEDATE, ZMESSAGETYPE, ZTEXT, ZFROMJID, ZPUSHNAME) "
            "VALUES (2, 1, ?, ?, ?, ?, ?, 2, NULL, ?, ?)",
            (
                chats[a.chat_jid], gm, int(a.is_from_me),
                f"DEMO_A{i:03d}", unix_to_cocoa(a.taken_unix),
                a.sender_jid, a.sender_name,
            ),
        )
        msg_pk = cur.lastrowid
        rel = f"Message/Media/{a.chat_jid}/{a.filename}"
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


def _write_wav(
    path: Path,
    tone_hz: int,
    duration_s: float,
    sample_rate: int = 8000,
) -> None:
    """Write a tiny mono 16-bit WAV with a sine tone — playable in browsers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    n_frames = int(sample_rate * duration_s)
    frames = bytearray()
    amplitude = 12000  # below clipping
    for i in range(n_frames):
        v = int(amplitude * math.sin(2 * math.pi * tone_hz * i / sample_rate))
        frames.extend(v.to_bytes(2, byteorder="little", signed=True))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(bytes(frames))


def _load_label_font() -> ImageFont.ImageFont:
    for candidate in _LABEL_FONTS:
        if Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, 64)
            except OSError:
                continue
    return ImageFont.load_default()
