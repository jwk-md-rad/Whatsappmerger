"""Merge a WhatsApp msgstore.db (source) into another (destination).

Strategy
========
We open the destination as the main connection and ``ATTACH`` the source as
``src``. All data motion happens inside SQLite — the merger never reads bulk
rows into Python — which is what makes very large backups (multi-GB) tractable.

For each interesting table we:

1. Build a temp mapping table (``jid_map``, ``chat_map``, ``message_map``)
   that maps ``src._id`` to the corresponding ``main._id``.
2. INSERT into main any rows whose natural key (raw_string for jids, jid for
   chats, the four-tuple for messages) does not already exist in main.
3. Populate the mapping table by joining src to main on the natural key.
4. For self-referential foreign keys (``message.quoted_row_id``), do a
   second-pass UPDATE once the message map exists.

We never overwrite rows that already exist in main (additive merge). The
caller can request a "prefer-newer" behavior with the ``prefer_newer`` flag.
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from .schema import (
    SchemaFlavor,
    column_intersection,
    detect_flavor,
    has_table,
    list_columns,
    message_satellite_tables,
    quote_cols,
)


log = logging.getLogger("whatsapp_merger")


@dataclass
class MergeReport:
    flavor: str = ""
    jids_inserted: int = 0
    chats_inserted: int = 0
    messages_inserted: int = 0
    messages_skipped_duplicate: int = 0
    satellite_rows_inserted: dict[str, int] = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    warnings: list[str] = field(default_factory=list)


def _set_perf_pragmas(conn: sqlite3.Connection) -> None:
    # We always operate on a freshly-copied destination, so durability isn't
    # critical mid-merge. The user can re-checksum the output afterwards.
    conn.execute("PRAGMA journal_mode = MEMORY")
    conn.execute("PRAGMA synchronous = OFF")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA cache_size = -262144")  # 256 MiB page cache
    conn.execute("PRAGMA foreign_keys = OFF")


def merge_databases(
    db_a: os.PathLike[str] | str,
    db_b: os.PathLike[str] | str,
    out: os.PathLike[str] | str,
    *,
    prefer_newer: bool = False,
) -> MergeReport:
    """Merge ``db_b`` into a copy of ``db_a``, write the result to ``out``.

    Returns a :class:`MergeReport` summarizing what changed. ``db_a`` and
    ``db_b`` are opened read-only — only the copy at ``out`` is modified.
    """
    db_a = Path(db_a)
    db_b = Path(db_b)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)

    log.info("Copying %s -> %s as merge base", db_a, out)
    shutil.copyfile(db_a, out)

    started = time.monotonic()
    report = MergeReport()

    conn = sqlite3.connect(out)
    try:
        _set_perf_pragmas(conn)
        conn.execute(f"ATTACH DATABASE '{db_b}' AS src")

        flavor_main = detect_flavor(conn)
        # detect on src by querying its sqlite_master
        flavor_src = _detect_flavor_attached(conn, "src")
        if flavor_main.name != flavor_src.name:
            raise ValueError(
                f"Schema flavor mismatch: destination is {flavor_main.name}, "
                f"source is {flavor_src.name}. Bring both to the same WhatsApp "
                "version before merging."
            )
        report.flavor = flavor_main.name

        conn.execute("BEGIN")
        try:
            if flavor_main.name == "modern":
                _merge_modern(conn, report, prefer_newer=prefer_newer)
            else:
                _merge_legacy(conn, report, prefer_newer=prefer_newer)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    report.elapsed_seconds = time.monotonic() - started
    return report


def _detect_flavor_attached(conn: sqlite3.Connection, schema: str) -> SchemaFlavor:
    tables = {
        r[0]
        for r in conn.execute(
            f"SELECT name FROM \"{schema}\".sqlite_master WHERE type='table'"
        )
    }
    if {"message", "chat", "jid"} <= tables:
        return SchemaFlavor("modern", "message", "chat", "jid")
    if "messages" in tables:
        return SchemaFlavor("legacy", "messages", None, None)
    raise ValueError(f"Unrecognized schema in {schema!r}")


# ---------------------------------------------------------------------------
# Modern (post-2022) schema
# ---------------------------------------------------------------------------

def _merge_modern(
    conn: sqlite3.Connection,
    report: MergeReport,
    *,
    prefer_newer: bool,
) -> None:
    _build_jid_map(conn, report)
    _build_chat_map(conn, report)
    _build_message_map(conn, report, prefer_newer=prefer_newer)
    _remap_message_self_refs(conn)
    _copy_message_satellites(conn, report)
    _copy_group_participants(conn, report)


def _insert_select(
    conn: sqlite3.Connection,
    *,
    table: str,
    cols: list[str],
    select: str,
    params: tuple = (),
) -> int:
    sql = (
        f'INSERT INTO main."{table}" ({quote_cols(cols)}) {select}'
    )
    cur = conn.execute(sql, params)
    return cur.rowcount


def _build_jid_map(conn: sqlite3.Connection, report: MergeReport) -> None:
    cols = column_intersection(conn, "jid")
    cols = [c for c in cols if c != "_id"]
    if "raw_string" not in cols:
        report.warnings.append("jid table lacks raw_string; skipping merge of jids")
        return

    conn.execute(
        "CREATE TEMP TABLE jid_map (src_id INTEGER PRIMARY KEY, dst_id INTEGER NOT NULL)"
    )
    inserted = _insert_select(
        conn,
        table="jid",
        cols=cols,
        select=(
            f'SELECT {quote_cols(cols)} FROM src.jid s '
            'WHERE s.raw_string IS NOT NULL '
            'AND NOT EXISTS (SELECT 1 FROM main.jid m WHERE m.raw_string = s.raw_string)'
        ),
    )
    report.jids_inserted = inserted

    conn.execute(
        "INSERT INTO jid_map (src_id, dst_id) "
        "SELECT s._id, m._id FROM src.jid s "
        "JOIN main.jid m ON m.raw_string = s.raw_string"
    )


def _build_chat_map(conn: sqlite3.Connection, report: MergeReport) -> None:
    if not has_table(conn, "chat"):
        return
    cols = column_intersection(conn, "chat")
    cols = [c for c in cols if c != "_id"]
    if "jid_row_id" not in cols:
        report.warnings.append("chat table lacks jid_row_id; skipping chat merge")
        return

    conn.execute(
        "CREATE TEMP TABLE chat_map (src_id INTEGER PRIMARY KEY, dst_id INTEGER NOT NULL)"
    )

    # Build the SELECT, substituting jid_row_id with the remapped id
    select_cols = [
        f'jm.dst_id AS jid_row_id' if c == "jid_row_id" else f'sc."{c}"'
        for c in cols
    ]
    inserted = _insert_select(
        conn,
        table="chat",
        cols=cols,
        select=(
            f"SELECT {', '.join(select_cols)} FROM src.chat sc "
            "JOIN jid_map jm ON jm.src_id = sc.jid_row_id "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM main.chat mc WHERE mc.jid_row_id = jm.dst_id"
            ")"
        ),
    )
    report.chats_inserted = inserted

    conn.execute(
        "INSERT INTO chat_map (src_id, dst_id) "
        "SELECT sc._id, mc._id FROM src.chat sc "
        "JOIN jid_map jm ON jm.src_id = sc.jid_row_id "
        "JOIN main.chat mc ON mc.jid_row_id = jm.dst_id"
    )


def _build_message_map(
    conn: sqlite3.Connection,
    report: MergeReport,
    *,
    prefer_newer: bool,
) -> None:
    cols = column_intersection(conn, "message")
    cols = [c for c in cols if c != "_id"]
    required = {"chat_row_id", "from_me", "key_id"}
    missing = required - set(cols)
    if missing:
        raise ValueError(f"message table missing required columns: {missing}")
    has_sender = "sender_jid_row_id" in cols
    has_quoted = "quoted_row_id" in cols

    conn.execute(
        "CREATE TEMP TABLE message_map (src_id INTEGER PRIMARY KEY, dst_id INTEGER NOT NULL)"
    )

    select_cols: list[str] = []
    for c in cols:
        if c == "chat_row_id":
            select_cols.append("cm.dst_id AS chat_row_id")
        elif c == "sender_jid_row_id":
            select_cols.append("sjm.dst_id AS sender_jid_row_id")
        elif c == "quoted_row_id":
            # remap in second pass; insert original NULL to avoid bad FK
            select_cols.append("NULL AS quoted_row_id")
        else:
            select_cols.append(f'sm."{c}"')

    sender_join = (
        "LEFT JOIN jid_map sjm ON sjm.src_id = sm.sender_jid_row_id"
        if has_sender
        else ""
    )
    sender_eq = (
        "AND IFNULL(mm.sender_jid_row_id, -1) = IFNULL(sjm.dst_id, -1)"
        if has_sender
        else ""
    )

    inserted = _insert_select(
        conn,
        table="message",
        cols=cols,
        select=(
            f"SELECT {', '.join(select_cols)} FROM src.message sm "
            "JOIN chat_map cm ON cm.src_id = sm.chat_row_id "
            f"{sender_join} "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM main.message mm "
            "  WHERE mm.chat_row_id = cm.dst_id "
            "    AND mm.from_me = sm.from_me "
            "    AND mm.key_id = sm.key_id "
            f"   {sender_eq}"
            ")"
        ),
    )
    report.messages_inserted = inserted

    sender_eq_for_map = (
        "AND IFNULL(mm.sender_jid_row_id, -1) = IFNULL(sjm.dst_id, -1)"
        if has_sender
        else ""
    )
    conn.execute(
        "INSERT INTO message_map (src_id, dst_id) "
        "SELECT sm._id, mm._id FROM src.message sm "
        "JOIN chat_map cm ON cm.src_id = sm.chat_row_id "
        f"{sender_join} "
        "JOIN main.message mm ON mm.chat_row_id = cm.dst_id "
        "  AND mm.from_me = sm.from_me "
        "  AND mm.key_id = sm.key_id "
        f"  {sender_eq_for_map}"
    )

    # report skipped count = src messages that found a match in main (i.e.
    # mapped but not inserted)
    total_mapped = conn.execute("SELECT COUNT(*) FROM message_map").fetchone()[0]
    report.messages_skipped_duplicate = max(0, total_mapped - inserted)

    if prefer_newer and "received_timestamp" in cols:
        # Update destination rows where source has a strictly newer
        # received_timestamp. Only updates non-key columns.
        updatable = [c for c in cols if c not in {
            "chat_row_id", "from_me", "key_id", "sender_jid_row_id",
        }]
        set_clause = ", ".join(
            f'"{c}" = (SELECT sm."{c}" FROM src.message sm '
            f'JOIN message_map mp ON mp.src_id = sm._id '
            f'WHERE mp.dst_id = main.message._id)'
            for c in updatable
        )
        conn.execute(
            f"UPDATE main.message SET {set_clause} "
            "WHERE _id IN ("
            "  SELECT mp.dst_id FROM message_map mp "
            "  JOIN src.message sm ON sm._id = mp.src_id "
            "  JOIN main.message mm ON mm._id = mp.dst_id "
            "  WHERE COALESCE(sm.received_timestamp, 0) > COALESCE(mm.received_timestamp, 0)"
            ")"
        )

    # Keep `quoted_row_id` for second pass
    conn.execute(
        "CREATE TEMP TABLE _pending_quoted (src_msg_id INTEGER PRIMARY KEY, src_quoted_id INTEGER)"
    )
    if has_quoted:
        conn.execute(
            "INSERT INTO _pending_quoted (src_msg_id, src_quoted_id) "
            "SELECT _id, quoted_row_id FROM src.message "
            "WHERE quoted_row_id IS NOT NULL"
        )


def _remap_message_self_refs(conn: sqlite3.Connection) -> None:
    # quoted_row_id second pass
    cols = list_columns(conn, "message")
    if "quoted_row_id" not in cols:
        return
    conn.execute(
        "UPDATE main.message SET quoted_row_id = ("
        "  SELECT mp_q.dst_id FROM _pending_quoted pq "
        "  JOIN message_map mp_src ON mp_src.src_id = pq.src_msg_id "
        "  JOIN message_map mp_q ON mp_q.src_id = pq.src_quoted_id "
        "  WHERE mp_src.dst_id = main.message._id"
        ") "
        "WHERE _id IN (SELECT mp.dst_id FROM message_map mp "
        "             JOIN _pending_quoted pq ON pq.src_msg_id = mp.src_id)"
    )


def _copy_message_satellites(conn: sqlite3.Connection, report: MergeReport) -> None:
    # Find tables in main that have message_row_id and exist in src too.
    candidates = set(message_satellite_tables(conn, "main")) & set(
        message_satellite_tables(conn, "src")
    )
    # message_media doesn't match the LIKE 'message\_%' pattern (it does, actually)
    for extra in ("message_media", "message_thumbnail", "message_quoted"):
        if has_table(conn, extra, "main") and has_table(conn, extra, "src"):
            cols = list_columns(conn, extra, "main")
            if "message_row_id" in cols:
                candidates.add(extra)

    for table in sorted(candidates):
        cols = column_intersection(conn, table)
        if "message_row_id" not in cols:
            continue
        # Build SELECT: replace message_row_id with mapped id; remap any
        # *_jid_row_id columns we can.
        select_cols: list[str] = []
        for c in cols:
            if c == "message_row_id":
                select_cols.append("mm.dst_id AS message_row_id")
            elif c == "chat_row_id":
                select_cols.append("cm.dst_id AS chat_row_id")
            elif c.endswith("_jid_row_id"):
                select_cols.append(f'jm_{c}.dst_id AS "{c}"')
            else:
                select_cols.append(f'st."{c}"')

        joins = ["JOIN message_map mm ON mm.src_id = st.message_row_id"]
        if "chat_row_id" in cols:
            joins.append("LEFT JOIN chat_map cm ON cm.src_id = st.chat_row_id")
        for c in cols:
            if c.endswith("_jid_row_id"):
                joins.append(
                    f'LEFT JOIN jid_map jm_{c} ON jm_{c}.src_id = st."{c}"'
                )

        # Skip rows that would collide on message_row_id PK (one-to-one tables).
        sql = (
            f'INSERT OR IGNORE INTO main."{table}" ({quote_cols(cols)}) '
            f"SELECT {', '.join(select_cols)} "
            f'FROM src."{table}" st {" ".join(joins)}'
        )
        try:
            cur = conn.execute(sql)
            report.satellite_rows_inserted[table] = cur.rowcount
        except sqlite3.OperationalError as e:
            report.warnings.append(f"satellite table {table!r}: {e}")


def _copy_group_participants(conn: sqlite3.Connection, report: MergeReport) -> None:
    table = "group_participant_user"
    if not (has_table(conn, table, "main") and has_table(conn, table, "src")):
        return
    cols = column_intersection(conn, table)
    cols = [c for c in cols if c != "_id"]
    if not {"group_jid_row_id", "user_jid_row_id"} <= set(cols):
        return
    select_cols = []
    for c in cols:
        if c == "group_jid_row_id":
            select_cols.append("gjm.dst_id AS group_jid_row_id")
        elif c == "user_jid_row_id":
            select_cols.append("ujm.dst_id AS user_jid_row_id")
        else:
            select_cols.append(f'sp."{c}"')
    sql = (
        f'INSERT OR IGNORE INTO main."{table}" ({quote_cols(cols)}) '
        f"SELECT {', '.join(select_cols)} "
        f'FROM src."{table}" sp '
        "JOIN jid_map gjm ON gjm.src_id = sp.group_jid_row_id "
        "JOIN jid_map ujm ON ujm.src_id = sp.user_jid_row_id"
    )
    try:
        cur = conn.execute(sql)
        report.satellite_rows_inserted[table] = cur.rowcount
    except sqlite3.OperationalError as e:
        report.warnings.append(f"{table}: {e}")


# ---------------------------------------------------------------------------
# Legacy (pre-2022) schema
# ---------------------------------------------------------------------------

def _merge_legacy(
    conn: sqlite3.Connection,
    report: MergeReport,
    *,
    prefer_newer: bool,
) -> None:
    cols = column_intersection(conn, "messages")
    cols = [c for c in cols if c != "_id"]
    required = {"key_remote_jid", "key_from_me", "key_id"}
    missing = required - set(cols)
    if missing:
        raise ValueError(f"legacy messages table missing required columns: {missing}")

    inserted = _insert_select(
        conn,
        table="messages",
        cols=cols,
        select=(
            f'SELECT {quote_cols(cols)} FROM src.messages s '
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM main.messages m "
            "  WHERE m.key_remote_jid = s.key_remote_jid "
            "    AND m.key_from_me = s.key_from_me "
            "    AND m.key_id = s.key_id"
            ")"
        ),
    )
    report.messages_inserted = inserted

    if prefer_newer and "received_timestamp" in cols:
        updatable = [c for c in cols if c not in {
            "key_remote_jid", "key_from_me", "key_id",
        }]
        set_clause = ", ".join(
            f'"{c}" = (SELECT s."{c}" FROM src.messages s '
            f'WHERE s.key_remote_jid = main.messages.key_remote_jid '
            f'  AND s.key_from_me = main.messages.key_from_me '
            f'  AND s.key_id = main.messages.key_id)'
            for c in updatable
        )
        conn.execute(
            f"UPDATE main.messages SET {set_clause} "
            "WHERE EXISTS ("
            "  SELECT 1 FROM src.messages s "
            "  WHERE s.key_remote_jid = main.messages.key_remote_jid "
            "    AND s.key_from_me = main.messages.key_from_me "
            "    AND s.key_id = main.messages.key_id "
            "    AND COALESCE(s.received_timestamp, 0) > COALESCE(main.messages.received_timestamp, 0)"
            ")"
        )
