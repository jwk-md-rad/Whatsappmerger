"""CLI for whatsapp-photos.

Commands::

    wa-photos ingest <ChatStorage.sqlite> <Media-root> -o photos.db
    wa-photos serve photos.db [--host 127.0.0.1 --port 8765]
    wa-photos password set photos.db
    wa-photos password clear photos.db
    wa-photos search photos.db "pizza" --since 2024-01-01

The ingest is the heaviest step; ``serve`` and ``search`` open the same
.db file read-only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

from . import __version__
from . import auth as auth_mod
from .ingest import IngestOptions, ingest
from .search import SearchFilters, search_photos


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wa-photos",
        description="Build and search a database of photos from a WhatsApp iOS backup.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    ing = sub.add_parser("ingest", help="Build photos.db from ChatStorage + Media")
    ing.add_argument("chat_db", help="Path to extracted ChatStorage.sqlite")
    ing.add_argument("media_root", help="Directory containing the WhatsApp Media tree")
    ing.add_argument("-o", "--output", required=True, help="Output photos.db path")
    ing.add_argument("--include-stickers", action="store_true")
    ing.add_argument("--no-gifs", action="store_true", help="Skip animated GIFs")
    ing.add_argument("--sha256", action="store_true", help="Compute SHA-256 per file (slower)")
    ing.add_argument(
        "--password",
        action="store_true",
        help="After ingest, prompt for a web-viewer password (recommended)",
    )

    srv = sub.add_parser("serve", help="Run the local web viewer")
    srv.add_argument("db", help="Path to photos.db")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8765)

    pw = sub.add_parser("password", help="Manage the web-viewer password")
    pw_sub = pw.add_subparsers(dest="pw_cmd", required=True)
    pw_set = pw_sub.add_parser("set", help="Set or change the password")
    pw_set.add_argument("db")
    pw_clear = pw_sub.add_parser("clear", help="Remove password protection")
    pw_clear.add_argument("db")
    pw_show = pw_sub.add_parser("status", help="Report whether a password is set")
    pw_show.add_argument("db")

    s = sub.add_parser("search", help="Search the database from the command line")
    s.add_argument("db")
    s.add_argument("query", nargs="?", default=None)
    s.add_argument("--chat-id", type=int)
    s.add_argument("--sender")
    s.add_argument("--since", help="YYYY-MM-DD")
    s.add_argument("--until", help="YYYY-MM-DD")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--offset", type=int, default=0)
    s.add_argument("--json", action="store_true", help="Emit JSON instead of a table")

    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.cmd == "ingest":
        return _cmd_ingest(args)
    if args.cmd == "serve":
        return _cmd_serve(args)
    if args.cmd == "password":
        return _cmd_password(args)
    if args.cmd == "search":
        return _cmd_search(args)
    return 1


def _cmd_ingest(args) -> int:
    opts = IngestOptions(
        include_stickers=args.include_stickers,
        include_gifs=not args.no_gifs,
        compute_sha256=args.sha256,
    )
    report = ingest(args.chat_db, args.media_root, args.output, options=opts)

    print(f"Ingested -> {args.output}")
    print(f"  Photos inserted:        {report.photos_inserted}")
    print(f"  Chats:                  {report.chats_inserted}")
    print(f"  Skipped (file missing): {report.rows_skipped_missing_file}")
    print(f"  Skipped (not image):    {report.rows_skipped_not_image}")
    print(f"  Skipped (unreadable):   {report.rows_skipped_unreadable}")
    print(f"  Media root anchor:      {report.media_root_used}")
    print(f"  Elapsed:                {report.elapsed_seconds:.2f}s")
    if report.warnings:
        print("  Warnings:")
        for w in report.warnings:
            print(f"    - {w}")

    if args.password:
        _set_password_interactive(Path(args.output))
    else:
        print("\nNo password set. The archive is unprotected.")
        print("Run `wa-photos password set " + args.output + "` to add one.")
    return 0


def _cmd_serve(args) -> int:
    import uvicorn

    from .web import create_app

    app = create_app(Path(args.db))
    print(f"Serving photo archive at http://{args.host}:{args.port}/")
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        if auth_mod.has_password(conn):
            print("(Password protection: ON — your browser will prompt for credentials.)")
        else:
            print("(Password protection: OFF — anyone on this host can read the archive.)")
    finally:
        conn.close()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def _cmd_password(args) -> int:
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"No such file: {db_path}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(db_path)
    try:
        if args.pw_cmd == "status":
            print("set" if auth_mod.has_password(conn) else "not set")
            return 0
        if args.pw_cmd == "clear":
            auth_mod.clear_password(conn)
            print("Password cleared. The archive is now unprotected.")
            return 0
        if args.pw_cmd == "set":
            _set_password_interactive_with_conn(conn)
            return 0
    finally:
        conn.close()
    return 1


def _set_password_interactive(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        _set_password_interactive_with_conn(conn)
    finally:
        conn.close()


def _set_password_interactive_with_conn(conn: sqlite3.Connection) -> None:
    while True:
        pw1 = getpass.getpass("New password: ")
        if not pw1:
            print("Empty password not allowed.", file=sys.stderr)
            continue
        pw2 = getpass.getpass("Confirm password: ")
        if pw1 != pw2:
            print("Passwords do not match. Try again.", file=sys.stderr)
            continue
        auth_mod.set_password(conn, pw1)
        print("Password set.")
        return


def _cmd_search(args) -> int:
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        filters = SearchFilters(
            query=args.query,
            chat_id=args.chat_id,
            sender_jid=args.sender,
            since_unix=_date_to_unix(args.since, end=False),
            until_unix=_date_to_unix(args.until, end=True),
            limit=args.limit,
            offset=args.offset,
        )
        hits = search_photos(conn, filters, order_by="newest")
    finally:
        conn.close()

    if args.json:
        print(json.dumps([_hit_to_dict(h) for h in hits], indent=2, default=str))
        return 0

    if not hits:
        print("No matches.")
        return 0
    for h in hits:
        ts = (
            dt.datetime.utcfromtimestamp(h.taken_at).strftime("%Y-%m-%d %H:%M")
            if h.taken_at else "?"
        )
        sender = "Me" if h.is_from_me else (h.sender_name or h.sender_jid or "?")
        chat = h.chat_name or h.chat_jid
        cap = (h.caption or "").replace("\n", " ")
        if len(cap) > 80:
            cap = cap[:79] + "…"
        print(f"#{h.id:<6} {ts}  {sender}  in {chat}  [{h.filename}]  {cap}")
    return 0


def _date_to_unix(s: str | None, *, end: bool) -> int | None:
    if not s:
        return None
    d = dt.datetime.strptime(s, "%Y-%m-%d")
    if end:
        d = d + dt.timedelta(days=1) - dt.timedelta(seconds=1)
    return int(d.replace(tzinfo=dt.timezone.utc).timestamp())


def _hit_to_dict(h) -> dict:
    return {
        "id": h.id,
        "taken_at": h.taken_at,
        "sender": h.sender_name or h.sender_jid,
        "from_me": h.is_from_me,
        "chat": h.chat_name or h.chat_jid,
        "filename": h.filename,
        "absolute_path": h.absolute_path,
        "width": h.width,
        "height": h.height,
        "file_size": h.file_size,
        "caption": h.caption,
    }


# Allow `python -m whatsapp_photos.cli`
if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

# silence unused-import in some checkers
_ = os
