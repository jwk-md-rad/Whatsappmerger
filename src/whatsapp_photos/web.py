"""FastAPI web viewer for the WhatsApp archive database.

Localhost-only by default. If a password has been set on the database, all
endpoints are gated behind HTTP Basic Auth that validates against the
PBKDF2 hash stored in the ``meta`` table.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth as auth_mod
from .ingest import _ro_uri
from .search import (
    SearchFilters,
    _row_to_hit,
    count_messages,
    list_chats,
    list_senders,
    search_messages,
)
from .thumbs import ensure_thumb


_MSG_COLS = (
    "id, chat_jid, chat_name, is_group, sender_jid, sender_name, "
    "is_from_me, sent_at, type, body, media_path, absolute_path, "
    "filename, file_size, width, height, mime_type"
)


PACKAGE_ROOT = Path(__file__).parent
TEMPLATE_DIR = PACKAGE_ROOT / "templates"
STATIC_DIR = PACKAGE_ROOT / "static"


def create_app(db_path: Path) -> FastAPI:
    db_path = Path(db_path).resolve()
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    cache_dir = db_path.parent / f"{db_path.stem}.thumbs"

    app = FastAPI(title="WhatsApp Archive")
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    basic = HTTPBasic(auto_error=False)

    def get_conn() -> sqlite3.Connection:
        c = sqlite3.connect(_ro_uri(db_path), uri=True)
        c.row_factory = sqlite3.Row
        return c

    def require_auth(
        credentials: HTTPBasicCredentials | None = Depends(basic),
    ) -> None:
        conn = get_conn()
        try:
            if not auth_mod.has_password(conn):
                return
            if credentials is None or not auth_mod.verify_password(
                conn, credentials.password
            ):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Authentication required",
                    headers={"WWW-Authenticate": 'Basic realm="WhatsApp Archive"'},
                )
        finally:
            conn.close()

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, _=Depends(require_auth)) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"title": "WhatsApp Archive"},
        )

    @app.get("/thread/{chat_id}", response_class=HTMLResponse)
    def thread(
        chat_id: int,
        request: Request,
        _=Depends(require_auth),
    ) -> HTMLResponse:
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT c.id, c.jid, c.name, c.is_group, "
                "COUNT(m.id) AS message_count "
                "FROM chats c LEFT JOIN messages m ON m.chat_id = c.id "
                "WHERE c.id = ? GROUP BY c.id",
                (chat_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise HTTPException(status_code=404, detail="Chat not found")
        return templates.TemplateResponse(
            request=request,
            name="thread.html",
            context={
                "chat_id": chat_id,
                "chat_name": row["name"] or row["jid"],
                "is_group": bool(row["is_group"]),
                "message_count": row["message_count"],
            },
        )

    @app.get("/api/search")
    def api_search(
        q: str | None = None,
        chat_id: int | None = None,
        sender: str | None = None,
        type: str | None = None,
        is_group: bool | None = None,
        is_from_me: bool | None = None,
        since: int | None = None,
        until: int | None = None,
        limit: int = Query(60, ge=1, le=500),
        offset: int = Query(0, ge=0),
        order: str = Query("newest"),
        _: None = Depends(require_auth),
    ) -> JSONResponse:
        conn = get_conn()
        try:
            filters = SearchFilters(
                query=q,
                chat_id=chat_id,
                sender_jid=sender,
                type=type,
                is_group=is_group,
                is_from_me=is_from_me,
                since_unix=since,
                until_unix=until,
                limit=limit,
                offset=offset,
            )
            total = count_messages(conn, filters)
            hits = search_messages(conn, filters, order_by=order)
        finally:
            conn.close()
        return JSONResponse(
            {
                "total": total,
                "results": [_hit_dict(h) for h in hits],
                "next_offset": offset + len(hits) if (offset + len(hits)) < total else None,
            }
        )

    @app.get("/api/chats")
    def api_chats(_: None = Depends(require_auth)) -> JSONResponse:
        conn = get_conn()
        try:
            return JSONResponse(list_chats(conn))
        finally:
            conn.close()

    @app.get("/api/senders")
    def api_senders(_: None = Depends(require_auth)) -> JSONResponse:
        conn = get_conn()
        try:
            return JSONResponse(list_senders(conn))
        finally:
            conn.close()

    @app.get("/api/photo/{message_id}")
    def api_photo(message_id: int, _: None = Depends(require_auth)) -> FileResponse:
        path, mime = _resolve_image(get_conn, message_id)
        return FileResponse(path, media_type=mime)

    @app.get("/api/media/{message_id}")
    def api_media(message_id: int, _: None = Depends(require_auth)) -> FileResponse:
        """Serve any media file (image, audio, video) for a message."""
        path, mime = _resolve_media(get_conn, message_id)
        return FileResponse(path, media_type=mime)

    @app.get("/api/thread/{chat_id}")
    def api_thread(
        chat_id: int,
        around: int | None = None,
        before: int | None = None,
        after: int | None = None,
        limit: int = Query(100, ge=1, le=500),
        _: None = Depends(require_auth),
    ) -> JSONResponse:
        """Paginate messages within a single chat.

        Modes (mutually exclusive):
        - ``around=<unix>`` → N/2 messages ≤ unix + N/2 strictly > unix.
        - ``before=<unix>`` → up to N messages strictly older than unix.
        - ``after=<unix>``  → up to N messages strictly newer than unix.
        - none of the above → the latest N messages (initial load).

        Result is always oldest-first.
        """
        conn = get_conn()
        try:
            if around is not None:
                half = limit // 2
                tail = limit - half
                before_rows = conn.execute(
                    f"SELECT {_MSG_COLS} FROM messages "
                    "WHERE chat_id=? AND sent_at IS NOT NULL AND sent_at <= ? "
                    "ORDER BY sent_at DESC, id DESC LIMIT ?",
                    (chat_id, around, half),
                ).fetchall()
                after_rows = conn.execute(
                    f"SELECT {_MSG_COLS} FROM messages "
                    "WHERE chat_id=? AND sent_at IS NOT NULL AND sent_at > ? "
                    "ORDER BY sent_at ASC, id ASC LIMIT ?",
                    (chat_id, around, tail),
                ).fetchall()
                ordered = list(reversed(before_rows)) + list(after_rows)
                payload = {
                    "messages": [_hit_dict(_row_to_hit(r)) for r in ordered],
                    "before_count": len(before_rows),
                }
            elif after is not None:
                rows = conn.execute(
                    f"SELECT {_MSG_COLS} FROM messages "
                    "WHERE chat_id=? AND sent_at IS NOT NULL AND sent_at > ? "
                    "ORDER BY sent_at ASC, id ASC LIMIT ?",
                    (chat_id, after, limit),
                ).fetchall()
                payload = {"messages": [_hit_dict(_row_to_hit(r)) for r in rows]}
            elif before is not None:
                rows = conn.execute(
                    f"SELECT {_MSG_COLS} FROM messages "
                    "WHERE chat_id=? AND sent_at IS NOT NULL AND sent_at < ? "
                    "ORDER BY sent_at DESC, id DESC LIMIT ?",
                    (chat_id, before, limit),
                ).fetchall()
                rows = list(reversed(rows))
                payload = {"messages": [_hit_dict(_row_to_hit(r)) for r in rows]}
            else:
                rows = conn.execute(
                    f"SELECT {_MSG_COLS} FROM messages "
                    "WHERE chat_id=? AND sent_at IS NOT NULL "
                    "ORDER BY sent_at DESC, id DESC LIMIT ?",
                    (chat_id, limit),
                ).fetchall()
                rows = list(reversed(rows))
                payload = {"messages": [_hit_dict(_row_to_hit(r)) for r in rows]}
        finally:
            conn.close()
        return JSONResponse(payload)

    @app.get("/api/histogram/{chat_id}")
    def api_histogram(chat_id: int, _: None = Depends(require_auth)) -> JSONResponse:
        """Per-month message counts for a single chat — feeds the scrubber."""
        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT strftime('%Y-%m', sent_at, 'unixepoch') AS bucket, "
                "MIN(sent_at) AS start_unix, MAX(sent_at) AS end_unix, "
                "COUNT(*) AS count "
                "FROM messages WHERE chat_id = ? AND sent_at IS NOT NULL "
                "GROUP BY bucket ORDER BY bucket",
                (chat_id,),
            ).fetchall()
        finally:
            conn.close()
        return JSONResponse([dict(r) for r in rows])

    @app.get("/api/thumb/{message_id}")
    def api_thumb(
        message_id: int,
        size: int = Query(240, ge=64, le=1024),
        _: None = Depends(require_auth),
    ) -> FileResponse:
        path, _mime = _resolve_image(get_conn, message_id)
        thumb = ensure_thumb(path, cache_dir, message_id, size=size)
        return FileResponse(thumb, media_type="image/jpeg")

    return app


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _hit_dict(h) -> dict:
    d = {
        "id": h.id,
        "chat_jid": h.chat_jid,
        "chat_name": h.chat_name,
        "is_group": h.is_group,
        "sender_jid": h.sender_jid,
        "sender_name": h.sender_name,
        "is_from_me": h.is_from_me,
        "sent_at": h.sent_at,
        "type": h.type,
        "body": h.body,
        "snippet": h.snippet,
    }
    if h.type == "image":
        d.update({
            "filename": h.filename,
            "file_size": h.file_size,
            "width": h.width,
            "height": h.height,
            "mime_type": h.mime_type,
            "thumb_url": f"/api/thumb/{h.id}",
            "photo_url": f"/api/photo/{h.id}",
            "media_url": f"/api/media/{h.id}",
        })
    elif h.type in ("audio", "video"):
        d.update({
            "filename": h.filename,
            "file_size": h.file_size,
            "mime_type": h.mime_type,
            "media_url": f"/api/media/{h.id}",
        })
    return d


def _resolve_image(get_conn, message_id: int) -> tuple[Path, str]:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT absolute_path, mime_type, type FROM messages WHERE id = ?",
            (message_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="Message not found")
    if row["type"] != "image" or not row["absolute_path"]:
        raise HTTPException(status_code=404, detail="Message has no image")
    p = Path(row["absolute_path"])
    if not p.is_file():
        raise HTTPException(status_code=410, detail="Image file missing on disk")
    return p, row["mime_type"] or "application/octet-stream"


def _resolve_media(get_conn, message_id: int) -> tuple[Path, str]:
    """Like _resolve_image but accepts any type with a media file."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT absolute_path, mime_type, type FROM messages WHERE id = ?",
            (message_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="Message not found")
    if not row["absolute_path"]:
        raise HTTPException(status_code=404, detail="Message has no media")
    p = Path(row["absolute_path"])
    if not p.is_file():
        raise HTTPException(status_code=410, detail="Media file missing on disk")
    return p, row["mime_type"] or "application/octet-stream"


# silence unused-import warnings
_ = Iterable
