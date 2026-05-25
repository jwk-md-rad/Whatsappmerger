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
from .ingest import IngestOptions, ingest, _ro_uri
from .search import SearchFilters, search_messages


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wa-photos",
        description="Build and search a searchable archive of messages and photos from a WhatsApp iOS backup.",
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
    ing.add_argument("--no-text", action="store_true",
                     help="Index only image messages, skip text-only messages")
    ing.add_argument("--no-audio", action="store_true", help="Skip voice notes / audio files")
    ing.add_argument("--no-video", action="store_true", help="Skip video files")
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

    dem = sub.add_parser(
        "demo",
        help="Build a synthetic archive and open the viewer (no real backup needed)",
    )
    dem.add_argument("--host", default="127.0.0.1")
    dem.add_argument("--port", type=int, default=8765)
    dem.add_argument(
        "--cache-dir",
        default=None,
        help="Where to put the demo data (default: ~/.cache/whatsapp_photos/demo)",
    )

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
    s.add_argument("--type", choices=["text", "image", "audio", "video"],
                   help="Limit to one message type")
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
    if args.cmd == "demo":
        return _cmd_demo(args)
    if args.cmd == "password":
        return _cmd_password(args)
    if args.cmd == "search":
        return _cmd_search(args)
    return 1


def _cmd_ingest(args) -> int:
    opts = IngestOptions(
        include_stickers=args.include_stickers,
        include_gifs=not args.no_gifs,
        include_text=not args.no_text,
        include_audio=not args.no_audio,
        include_video=not args.no_video,
        compute_sha256=args.sha256,
    )
    report = ingest(args.chat_db, args.media_root, args.output, options=opts)

    print(f"Ingested -> {args.output}")
    print(f"  Images inserted:        {report.images_inserted:,}")
    print(f"  Text messages inserted: {report.texts_inserted:,}")
    print(f"  Voice notes / audio:    {report.audios_inserted:,}")
    print(f"  Videos inserted:        {report.videos_inserted:,}")
    print(f"  Chats:                  {report.chats_inserted}")
    print(f"  Skipped (file missing): {report.rows_skipped_missing_file}")
    print(f"  Skipped (unsupported):  {report.rows_skipped_not_image}")
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
    return _serve(Path(args.db), host=args.host, port=args.port)


def _cmd_demo(args) -> int:
    from . import demo as demo_mod
    from .ingest import ingest

    cache_dir = Path(args.cache_dir) if args.cache_dir else _default_demo_dir()
    print(f"Building demo data under {cache_dir} ...")
    chat_db, media_root = demo_mod.build_demo_fixture(cache_dir)
    photos_db = cache_dir / "photos.db"
    report = ingest(chat_db, media_root, photos_db)
    print(
        f"Built {report.images_inserted} demo photos and "
        f"{report.texts_inserted} text messages across "
        f"{report.chats_inserted} chats."
    )
    print()
    return _serve(photos_db, host=args.host, port=args.port)


def _serve(db_path: Path, *, host: str, port: int) -> int:
    import uvicorn

    from . import netinfo
    from .web import create_app

    _maybe_upgrade_indexes(db_path)
    app = create_app(db_path)
    print(f"Serving WhatsApp archive on {netinfo.url_for(host, port)}")
    if host in {"0.0.0.0", "::", ""}:
        lan = netinfo.primary_lan_ip()
        if lan:
            print(f"  LAN: {netinfo.url_for(lan, port)}")
        for label, peer in netinfo.tailscale_endpoints():
            print(f"  {label}: {netinfo.url_for(peer, port)}")
    else:
        print("  (bound to one interface; pass --host 0.0.0.0 to expose to your LAN/Tailscale)")

    conn = sqlite3.connect(_ro_uri(db_path), uri=True)
    try:
        if auth_mod.has_password(conn):
            print("(Password protection: ON — your browser will prompt for credentials.)")
        else:
            print("(Password protection: OFF — anyone who can reach this server can read the archive.)")
    finally:
        conn.close()
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


def _default_demo_dir() -> Path:
    return Path.home() / ".cache" / "whatsapp_photos" / "demo"


def _maybe_upgrade_indexes(db_path: Path) -> None:
    """Create any indexes that newer versions of the schema rely on.

    Old databases built before composite indexes were added would otherwise
    make the chat-list query slow on large archives. We try-and-shrug here
    rather than aborting startup: if the DB is read-only, locked, or
    otherwise can't accept the CREATE INDEX, the server still starts and
    serves correctly, just with the slower chat-list query.
    """
    from .schema import ensure_indexes

    if not db_path.exists():
        return
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error:
        return
    try:
        try:
            existing = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
        except sqlite3.Error:
            return
        wanted = {"messages_chat_sent", "messages_chat_image_sent"}
        missing = wanted - existing
        if not missing:
            return
        try:
            print(
                f"One-time index build for faster chat list "
                f"({', '.join(sorted(missing))})…"
            )
            ensure_indexes(conn)
            conn.commit()
            print("Indexes built.")
        except sqlite3.Error as e:
            print(
                f"Note: could not build chat-list index ({e}); the chat "
                f"list may be slow. Proceeding anyway.",
                file=sys.stderr,
            )
    finally:
        conn.close()


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
    conn = sqlite3.connect(_ro_uri(args.db), uri=True)
    try:
        filters = SearchFilters(
            query=args.query,
            chat_id=args.chat_id,
            sender_jid=args.sender,
            type=args.type,
            since_unix=_date_to_unix(args.since, end=False),
            until_unix=_date_to_unix(args.until, end=True),
            limit=args.limit,
            offset=args.offset,
        )
        hits = search_messages(conn, filters, order_by="newest")
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
            dt.datetime.utcfromtimestamp(h.sent_at).strftime("%Y-%m-%d %H:%M")
            if h.sent_at else "?"
        )
        sender = "Me" if h.is_from_me else (h.sender_name or h.sender_jid or "?")
        chat = h.chat_name or h.chat_jid
        body = (h.body or "").replace("\n", " ")
        if len(body) > 80:
            body = body[:79] + "…"
        tag = {"image": "[IMG]", "text": "[TXT]", "audio": "[AUD]", "video": "[VID]"}.get(
            h.type, f"[{h.type[:3].upper()}]"
        )
        print(f"#{h.id:<6} {ts}  {tag}  {sender}  in {chat}  {body}")
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
        "sent_at": h.sent_at,
        "type": h.type,
        "sender": h.sender_name or h.sender_jid,
        "from_me": h.is_from_me,
        "chat": h.chat_name or h.chat_jid,
        "filename": h.filename,
        "absolute_path": h.absolute_path,
        "width": h.width,
        "height": h.height,
        "file_size": h.file_size,
        "body": h.body,
    }


# Allow `python -m whatsapp_photos.cli`
if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

# silence unused-import in some checkers
_ = os
