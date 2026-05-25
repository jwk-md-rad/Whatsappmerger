"""Ingest messages from a WhatsApp iOS ChatStorage.sqlite + Message/Media tree.

Pipeline (one pass):

1. Query ZWAMESSAGE joined to ZWACHATSESSION, ZWAGROUPMEMBER and (LEFT)
   ZWAMEDIAITEM. We pull every message that has either a non-empty
   ``ZTEXT`` body or a ``ZMEDIALOCALPATH`` we can resolve.
2. Resolve each ``ZMEDIALOCALPATH`` against the user-provided media root.
   We auto-detect the right anchor point by trying a few candidates and
   picking the one that resolves the most paths.
3. Classify each row:
     - media that opens as an image (Pillow recognises it) → type='image'
     - otherwise, if body is non-empty → type='text'
     - otherwise → skipped (videos, audio, documents, expired media, …)
4. Insert into ``messages``.

After all rows are inserted, the FTS index is rebuilt from scratch.

The chat database is opened read-only (``mode=ro`` URI) so we never touch
the user's source files.
"""
from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from PIL import Image, UnidentifiedImageError

from .cocoa import cocoa_to_unix
from .schema import init_schema, rebuild_fts, refresh_chat_sender_names


log = logging.getLogger("whatsapp_photos.ingest")


# Pillow-recognised formats we treat as photos. Stickers are excluded by
# default (they're images, but cluttering); user can re-enable.
DEFAULT_PHOTO_FORMATS = {"JPEG", "PNG", "WEBP", "HEIF", "HEIC"}
ANIMATED_FORMATS = {"GIF"}

# Audio / video classification by file extension. WhatsApp voice notes
# are .opus inside an Ogg container; iMazing also surfaces .m4a/.mp3
# for shared audio. Videos are almost always .mp4 from iOS.
_AUDIO_MIME = {
    ".opus": "audio/ogg",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".aac": "audio/aac",
    ".wav": "audio/wav",
    ".caf": "audio/x-caf",
}
_VIDEO_MIME = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".3gp": "video/3gpp",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
}


def _classify_media_by_ext(path: Path) -> tuple[str, str] | None:
    """Return ``(type, mime)`` for an audio/video file, else None."""
    ext = path.suffix.lower()
    if ext in _AUDIO_MIME:
        return "audio", _AUDIO_MIME[ext]
    if ext in _VIDEO_MIME:
        return "video", _VIDEO_MIME[ext]
    return None


@dataclass
class IngestOptions:
    include_gifs: bool = True
    include_stickers: bool = False
    include_text: bool = True
    include_audio: bool = True
    include_video: bool = True
    compute_sha256: bool = False


@dataclass
class IngestReport:
    images_inserted: int = 0
    texts_inserted: int = 0
    audios_inserted: int = 0
    videos_inserted: int = 0
    rows_skipped_missing_file: int = 0
    rows_skipped_not_image: int = 0
    rows_skipped_unreadable: int = 0
    rows_skipped_empty: int = 0
    chats_inserted: int = 0
    media_root_used: str = ""
    elapsed_seconds: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def photos_inserted(self) -> int:
        """Back-compat alias for callers still reading the old field name."""
        return self.images_inserted


def _ro_uri(db_path: os.PathLike[str] | str) -> str:
    """Build a ``file:`` URI for read-only SQLite open.

    ``Path.as_uri()`` percent-encodes spaces and other characters that
    macOS / Windows users routinely have in their paths (think
    ``Mobile Documents``, ``Whatsapp Jar``). Plain f-string interpolation
    breaks SQLite's URI parser on those paths.
    """
    return f"{Path(db_path).resolve().as_uri()}?mode=ro"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def ingest(
    chat_db: os.PathLike[str] | str,
    media_root: os.PathLike[str] | str,
    out_db: os.PathLike[str] | str,
    *,
    options: IngestOptions | None = None,
) -> IngestReport:
    """Build a searchable message database from an iOS WhatsApp backup.

    ``chat_db`` is the extracted ``ChatStorage.sqlite``; ``media_root`` is
    the directory that contains the WhatsApp media tree (typically the
    parent of ``Message/`` after extraction with iMazing). ``out_db`` is the
    SQLite file to create or overwrite.
    """
    import time

    started = time.monotonic()
    options = options or IngestOptions()
    media_root = Path(media_root)
    out_db = Path(out_db)
    out_db.parent.mkdir(parents=True, exist_ok=True)

    # Carry the existing auth meta (salt + hash + iterations) across a
    # re-ingest so the user doesn't have to re-set their password every
    # time they refresh from a new iPhone backup.
    preserved_meta = _read_meta_for_carry(out_db) if out_db.exists() else []

    if out_db.exists():
        out_db.unlink()

    src = sqlite3.connect(_ro_uri(chat_db), uri=True)
    dst = sqlite3.connect(out_db)
    try:
        dst.execute("PRAGMA journal_mode = MEMORY")
        dst.execute("PRAGMA synchronous = OFF")
        init_schema(dst)

        anchor = _detect_media_anchor(src, media_root)
        report = IngestReport(media_root_used=str(anchor))
        log.info("Resolved media root anchor: %s", anchor)

        chat_cache: dict[str, int] = {}
        for row in _iter_message_rows(src):
            _process_row(row, anchor, chat_cache, dst, options, report)

        rebuild_fts(dst)
        refresh_chat_sender_names(dst)
        report.chats_inserted = dst.execute("SELECT COUNT(*) FROM chats").fetchone()[0]

        if preserved_meta:
            _write_meta(dst, preserved_meta)

        # Let the planner know what's in the partial indexes so it
        # actually picks them for global "newest images" queries
        # instead of falling back to messages_type + temp-b-tree sort.
        dst.execute("ANALYZE")
        dst.commit()
    finally:
        src.close()
        dst.close()

    report.elapsed_seconds = time.monotonic() - started
    return report


# ---------------------------------------------------------------------------
# Media-root auto-detect
# ---------------------------------------------------------------------------


def _detect_media_anchor(src: sqlite3.Connection, root: Path) -> Path:
    """Pick the directory that, joined with ``ZMEDIALOCALPATH``, finds files.

    Real iOS exports vary: sometimes the user gives us the directory
    containing ``Message/``, sometimes the ``Message/Media/`` directory
    itself, sometimes the Documents directory. Try a few candidates and
    pick the one that resolves the most paths.
    """
    sample = [
        r[0]
        for r in src.execute(
            "SELECT ZMEDIALOCALPATH FROM ZWAMEDIAITEM "
            "WHERE ZMEDIALOCALPATH IS NOT NULL LIMIT 50"
        ).fetchall()
    ]
    if not sample:
        return root

    candidates: list[Path] = [root]
    for sub in ("Library", "Documents", "Message", "Library/Message"):
        p = root / sub
        if p.exists():
            candidates.append(p)
    for path in sample[:5]:
        parts = Path(path).parts
        if parts and (root / "/".join(parts[1:])).parent.exists():
            candidates.append(root)

    best: tuple[int, Path] = (-1, root)
    for cand in candidates:
        hits = sum(1 for p in sample if (cand / p).exists())
        if hits > best[0]:
            best = (hits, cand)
    return best[1]


# ---------------------------------------------------------------------------
# Row iteration
# ---------------------------------------------------------------------------


_MESSAGE_QUERY = """
SELECT
    msg.Z_PK            AS msg_pk,
    msg.ZSTANZAID       AS stanza_id,
    msg.ZISFROMME       AS is_from_me,
    msg.ZMESSAGEDATE    AS msg_date_cocoa,
    msg.ZTEXT           AS body,
    msg.ZFROMJID        AS from_jid,
    msg.ZPUSHNAME       AS push_name,
    msg.ZMESSAGETYPE    AS raw_type,
    chat.Z_PK           AS chat_pk,
    chat.ZCONTACTJID    AS chat_jid,
    chat.ZSESSIONTYPE   AS session_type,
    chat.ZPARTNERNAME   AS chat_name,
    gm.ZMEMBERJID       AS group_member_jid,
    gm.ZCONTACTNAME     AS group_member_name,
    media.ZMEDIALOCALPATH AS media_path,
    media.ZFILESIZE     AS file_size_db
FROM ZWAMESSAGE msg
JOIN ZWACHATSESSION chat ON chat.Z_PK = msg.ZCHATSESSION
LEFT JOIN ZWAMEDIAITEM media ON media.ZMESSAGE = msg.Z_PK
LEFT JOIN ZWAGROUPMEMBER gm ON gm.Z_PK = msg.ZGROUPMEMBER
WHERE media.ZMEDIALOCALPATH IS NOT NULL
   OR (msg.ZTEXT IS NOT NULL AND length(msg.ZTEXT) > 0)
"""


def _iter_message_rows(src: sqlite3.Connection) -> Iterator[sqlite3.Row]:
    src.row_factory = sqlite3.Row
    yield from src.execute(_MESSAGE_QUERY)


# ---------------------------------------------------------------------------
# Row processing
# ---------------------------------------------------------------------------


def _process_row(
    row: sqlite3.Row,
    anchor: Path,
    chat_cache: dict[str, int],
    dst: sqlite3.Connection,
    options: IngestOptions,
    report: IngestReport,
) -> None:
    media_path = row["media_path"]
    body = row["body"]

    abs_path: Path | None = None
    image_info: tuple[str, str, int | None, int | None] | None = None

    if media_path:
        abs_path = (anchor / media_path).resolve()
        if not abs_path.is_file():
            report.rows_skipped_missing_file += 1
            abs_path = None
        else:
            image_info = _probe_image(abs_path)
            if image_info is not None:
                fmt = image_info[0]
                ok = fmt in DEFAULT_PHOTO_FORMATS or (
                    fmt in ANIMATED_FORMATS and options.include_gifs
                )
                if not ok:
                    image_info = None

    if image_info is not None and abs_path is not None:
        _insert_image(row, abs_path, image_info, chat_cache, dst, options, report)
        return

    # Not an image but the file is there → try audio / video by extension.
    if abs_path is not None:
        media_kind = _classify_media_by_ext(abs_path)
        if media_kind is not None:
            kind, mime = media_kind
            if (kind == "audio" and options.include_audio) or (
                kind == "video" and options.include_video
            ):
                _insert_media(row, abs_path, kind, mime, chat_cache, dst, options, report)
                return

    # Fall back to text body if available.
    if options.include_text and body and body.strip():
        _insert_text(row, chat_cache, dst, report)
        return

    # Row had unrecognised media (document/sticker, or future format) and
    # no usable text body — count it as skipped.
    if media_path and abs_path is not None:
        report.rows_skipped_not_image += 1
    elif not (body and body.strip()):
        report.rows_skipped_empty += 1


def _insert_image(
    row: sqlite3.Row,
    abs_path: Path,
    image_info: tuple[str, str, int | None, int | None],
    chat_cache: dict[str, int],
    dst: sqlite3.Connection,
    options: IngestOptions,
    report: IngestReport,
) -> None:
    fmt, mime, w, h = image_info
    chat_id = _ensure_chat(dst, chat_cache, row)
    sender_jid, sender_name = _resolve_sender(row)
    file_size = abs_path.stat().st_size
    sha = _sha256(abs_path) if options.compute_sha256 else None

    dst.execute(
        """
        INSERT OR IGNORE INTO messages (
            chat_id, chat_jid, chat_name, is_group,
            sender_jid, sender_name, is_from_me,
            stanza_id, sent_at,
            type, body,
            media_path, absolute_path, filename,
            file_size, sha256, width, height, mime_type
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            chat_id,
            row["chat_jid"],
            row["chat_name"],
            1 if (row["session_type"] or 0) == 1 else 0,
            sender_jid,
            sender_name,
            int(row["is_from_me"] or 0),
            row["stanza_id"],
            cocoa_to_unix(row["msg_date_cocoa"]),
            "image",
            row["body"],
            row["media_path"],
            str(abs_path),
            abs_path.name,
            file_size,
            sha,
            w,
            h,
            mime,
        ),
    )
    if dst.total_changes:
        report.images_inserted += 1


def _insert_media(
    row: sqlite3.Row,
    abs_path: Path,
    kind: str,
    mime: str,
    chat_cache: dict[str, int],
    dst: sqlite3.Connection,
    options: IngestOptions,
    report: IngestReport,
) -> None:
    """Insert an audio or video message row.

    Body holds the (rare) caption; width/height/sha256 stay NULL — we
    don't probe duration here to keep the ingest dependency-free.
    """
    chat_id = _ensure_chat(dst, chat_cache, row)
    sender_jid, sender_name = _resolve_sender(row)
    file_size = abs_path.stat().st_size
    sha = _sha256(abs_path) if options.compute_sha256 else None

    dst.execute(
        """
        INSERT OR IGNORE INTO messages (
            chat_id, chat_jid, chat_name, is_group,
            sender_jid, sender_name, is_from_me,
            stanza_id, sent_at,
            type, body,
            media_path, absolute_path, filename,
            file_size, sha256, mime_type
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            chat_id,
            row["chat_jid"],
            row["chat_name"],
            1 if (row["session_type"] or 0) == 1 else 0,
            sender_jid,
            sender_name,
            int(row["is_from_me"] or 0),
            row["stanza_id"],
            cocoa_to_unix(row["msg_date_cocoa"]),
            kind,
            row["body"],
            row["media_path"],
            str(abs_path),
            abs_path.name,
            file_size,
            sha,
            mime,
        ),
    )
    if dst.total_changes:
        if kind == "audio":
            report.audios_inserted += 1
        elif kind == "video":
            report.videos_inserted += 1


def _insert_text(
    row: sqlite3.Row,
    chat_cache: dict[str, int],
    dst: sqlite3.Connection,
    report: IngestReport,
) -> None:
    chat_id = _ensure_chat(dst, chat_cache, row)
    sender_jid, sender_name = _resolve_sender(row)

    dst.execute(
        """
        INSERT OR IGNORE INTO messages (
            chat_id, chat_jid, chat_name, is_group,
            sender_jid, sender_name, is_from_me,
            stanza_id, sent_at,
            type, body
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            chat_id,
            row["chat_jid"],
            row["chat_name"],
            1 if (row["session_type"] or 0) == 1 else 0,
            sender_jid,
            sender_name,
            int(row["is_from_me"] or 0),
            row["stanza_id"],
            cocoa_to_unix(row["msg_date_cocoa"]),
            "text",
            row["body"],
        ),
    )
    if dst.total_changes:
        report.texts_inserted += 1


def _ensure_chat(
    dst: sqlite3.Connection,
    cache: dict[str, int],
    row: sqlite3.Row,
) -> int:
    jid = row["chat_jid"]
    if jid in cache:
        return cache[jid]
    is_group = 1 if (row["session_type"] or 0) == 1 else 0
    dst.execute(
        "INSERT OR IGNORE INTO chats (jid, name, is_group) VALUES (?, ?, ?)",
        (jid, row["chat_name"], is_group),
    )
    chat_id = dst.execute(
        "SELECT id FROM chats WHERE jid = ?", (jid,)
    ).fetchone()[0]
    cache[jid] = chat_id
    return chat_id


def _resolve_sender(row: sqlite3.Row) -> tuple[str | None, str | None]:
    is_from_me = int(row["is_from_me"] or 0)
    if is_from_me:
        return None, "Me"
    if row["group_member_jid"]:
        return row["group_member_jid"], row["group_member_name"] or row["push_name"]
    return row["from_jid"], row["push_name"] or row["chat_name"]


def _probe_image(path: Path) -> tuple[str, str, int | None, int | None] | None:
    """Return ``(format, mime, width, height)`` or None if unreadable."""
    try:
        with Image.open(path) as im:
            fmt = (im.format or "").upper()
            mime = Image.MIME.get(fmt, "application/octet-stream")
            w, h = im.size
        return fmt, mime, w, h
    except (UnidentifiedImageError, OSError):
        return None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Re-ingest: preserve auth meta across DB replacement
# ---------------------------------------------------------------------------


_AUTH_META_KEYS = ("auth.salt", "auth.hash", "auth.iterations")


def _read_meta_for_carry(db_path: Path) -> list[tuple[str, bytes]]:
    """Pull auth.* keys out of an existing DB so re-ingest can re-apply
    them. Returns an empty list if the meta table or the keys are missing
    — both fine, just means there was no password."""
    try:
        conn = sqlite3.connect(_ro_uri(db_path), uri=True)
    except sqlite3.Error:
        return []
    try:
        try:
            rows = conn.execute(
                "SELECT key, value FROM meta WHERE key IN ("
                + ",".join("?" * len(_AUTH_META_KEYS))
                + ")",
                _AUTH_META_KEYS,
            ).fetchall()
        except sqlite3.Error:
            return []
    finally:
        conn.close()
    return [(k, v) for k, v in rows if v is not None]


def _write_meta(conn: sqlite3.Connection, items: list[tuple[str, bytes]]) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value BLOB)"
    )
    conn.executemany(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        items,
    )
