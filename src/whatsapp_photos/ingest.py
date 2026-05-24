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
from .schema import init_schema, rebuild_fts


log = logging.getLogger("whatsapp_photos.ingest")


# Pillow-recognised formats we treat as photos. Stickers are excluded by
# default (they're images, but cluttering); user can re-enable.
DEFAULT_PHOTO_FORMATS = {"JPEG", "PNG", "WEBP", "HEIF", "HEIC"}
ANIMATED_FORMATS = {"GIF"}


@dataclass
class IngestOptions:
    include_gifs: bool = True
    include_stickers: bool = False
    include_text: bool = True
    compute_sha256: bool = False


@dataclass
class IngestReport:
    images_inserted: int = 0
    texts_inserted: int = 0
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

    if out_db.exists():
        out_db.unlink()

    src = sqlite3.connect(f"file:{chat_db}?mode=ro", uri=True)
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
        report.chats_inserted = dst.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
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

    # Not an image (or media not usable). Fall back to text if there is a
    # body, otherwise drop the row.
    if options.include_text and body and body.strip():
        _insert_text(row, chat_cache, dst, report)
        return

    # Row had unreadable media (e.g. video, audio, doc, expired media) and
    # no usable text body — skip it.
    if media_path and image_info is None and abs_path is not None:
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
