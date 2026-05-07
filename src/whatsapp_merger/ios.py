"""iOS WhatsApp ChatStorage.sqlite (Core Data) merge adapter.

The iOS schema is laid out by Core Data and uses ``Z*`` table/column names
instead of WhatsApp's own. This module mirrors the Android merger
(``ATTACH`` + temp ID-mapping tables + dependency-ordered inserts + column
intersection) but adds four Core-Data-specific responsibilities:

1. **Entity-id remapping.** Each row carries ``Z_ENT`` referring to a row in
   ``Z_PRIMARYKEY`` keyed by entity name (``WAMessage``, ``WAChatSession``,
   …). Different WhatsApp versions assign different ``Z_ENT`` integers, so
   we build a per-entity remap table by joining src→dst on ``Z_NAME``.
2. **``Z_PRIMARYKEY.Z_MAX`` bookkeeping.** After inserting N rows for an
   entity we bump its ``Z_MAX`` so Core Data hands out unique ``Z_PK`` on
   the next save.
3. **Inverse relationships.** Core Data keeps both sides of a 1-to-1 (e.g.
   ``ZWAMESSAGE.ZMEDIAITEM`` <-> ``ZWAMEDIAITEM.ZMESSAGE``). We populate
   the message-side reference in a second pass after the media table is
   filled in.
4. **Denormalized ``ZWACHATSESSION`` fields.** ``ZLASTMESSAGE``,
   ``ZLASTMESSAGEDATE``, ``ZMESSAGECOUNTER`` are recomputed from the actual
   message rows post-merge, otherwise the chat list shows stale previews.

The merge writes into A's Core Data model verbatim (``Z_METADATA`` and
``Z_MODELCACHE`` are A's). When A and B come from different WhatsApp
versions, **A must be the newer one** so the merged file remains openable
by the device that produced A.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING

from .schema import column_intersection, has_table, list_columns, quote_cols

if TYPE_CHECKING:  # pragma: no cover
    from .merger import MergeReport


log = logging.getLogger("whatsapp_merger.ios")


# Core Data internal tables we never touch directly.
_CORE_DATA_INTERNAL = {
    "Z_PRIMARYKEY",
    "Z_METADATA",
    "Z_MODELCACHE",
}


# Known FK column → referenced entity-table. Used to drive remapping when we
# discover satellite tables we haven't seen before.
_FK_PATTERNS: dict[str, str] = {
    "ZMESSAGE": "ZWAMESSAGE",
    "ZCHATSESSION": "ZWACHATSESSION",
    "ZGROUPMEMBER": "ZWAGROUPMEMBER",
    "ZMEDIAITEM": "ZWAMEDIAITEM",
    "ZGROUPINFO": "ZWAGROUPINFO",
    "ZPARENTMESSAGE": "ZWAMESSAGE",
    "ZQUOTEDMESSAGE": "ZWAMESSAGE",
}


def merge_ios(
    conn: sqlite3.Connection,
    report: "MergeReport",
    *,
    allow_model_hash_mismatch: bool,
) -> None:
    _check_model_hashes(conn, report, allow_model_hash_mismatch)
    entity_map = _build_entity_map(conn, report)

    _build_chat_map(conn, report, entity_map)
    _build_groupmember_map(conn, report, entity_map)
    _build_message_map(conn, report, entity_map)
    _build_media_map(conn, report, entity_map)
    _fix_message_to_media_inverse(conn)
    _copy_other_satellites(conn, report, entity_map)
    _recompute_chatsession_denorm(conn)
    _bump_z_primarykey(conn, report)
    _verify_integrity(conn, report)


# ---------------------------------------------------------------------------
# Cross-version compatibility checks
# ---------------------------------------------------------------------------


def _check_model_hashes(
    conn: sqlite3.Connection,
    report: "MergeReport",
    allow_mismatch: bool,
) -> None:
    """Compare Core Data model hashes between the two stores.

    Z_METADATA holds a serialized plist with ``NSStoreModelVersionHashes``.
    We cheap-check by comparing the raw bytes — different model = different
    bytes. A mismatch means the two backups were taken from materially
    different WhatsApp builds; the merged file will use A's model and
    columns missing from A will simply be dropped.
    """
    main = conn.execute(
        "SELECT Z_PLIST FROM main.Z_METADATA WHERE ROWID=1"
    ).fetchone()
    src = conn.execute(
        "SELECT Z_PLIST FROM src.Z_METADATA WHERE ROWID=1"
    ).fetchone()
    if not main or not src:
        report.warnings.append("Z_METADATA missing in one store; cannot compare model hashes")
        return
    match = main[0] == src[0]
    report.ios_model_hashes_match = match
    if match:
        return
    msg = (
        "Core Data model hashes differ between A and B. Merging into A's "
        "model; columns present only in B will be dropped. Verify the merged "
        "file opens before re-injecting via iMazing."
    )
    if not allow_mismatch:
        raise ValueError(
            msg + " Pass --allow-model-hash-mismatch to proceed (and ensure "
            "A is the newer-version backup)."
        )
    report.warnings.append(msg)


def _build_entity_map(
    conn: sqlite3.Connection,
    report: "MergeReport",
) -> dict[str, tuple[int, int]]:
    """Map each entity name to (src Z_ENT, dst Z_ENT)."""
    rows = conn.execute(
        "SELECT m.Z_NAME, s.Z_ENT, m.Z_ENT "
        "FROM main.Z_PRIMARYKEY m JOIN src.Z_PRIMARYKEY s ON s.Z_NAME = m.Z_NAME"
    ).fetchall()
    out: dict[str, tuple[int, int]] = {}
    for name, src_ent, dst_ent in rows:
        out[name] = (src_ent, dst_ent)
        if src_ent != dst_ent:
            report.ios_entity_id_remaps[name] = (src_ent, dst_ent)
    return out


def _ent_for(table: str) -> str:
    """Strip the leading ``Z`` from a table name to get the Core Data entity name."""
    # Core Data table is "Z" + entity-name-uppercase, e.g. ZWAMESSAGE -> WAMESSAGE
    # Z_PRIMARYKEY.Z_NAME stores the original CamelCase name, e.g. "WAMessage".
    # Lookup is case-insensitive in our entity map: callers pass the actual
    # Z_NAME, not a derived value.
    raise NotImplementedError("use the entity_map values directly")


def _entity_name_for_table(conn: sqlite3.Connection, table: str) -> str | None:
    """Find the Z_PRIMARYKEY.Z_NAME for a Core Data table.

    The table-to-entity mapping isn't stored explicitly in SQLite, but the
    convention is ``Z<UPPERCASE(Z_NAME)>``. We scan Z_PRIMARYKEY and return
    the Z_NAME whose uppercase form matches the table's suffix.
    """
    suffix = table[1:] if table.startswith("Z") else table
    rows = conn.execute("SELECT Z_NAME FROM main.Z_PRIMARYKEY").fetchall()
    for (name,) in rows:
        if name.upper() == suffix:
            return name
    return None


# ---------------------------------------------------------------------------
# Per-entity merges
# ---------------------------------------------------------------------------


def _z_cols(cols: list[str]) -> list[str]:
    """Drop the auto-managed primary key from an insert column list."""
    return [c for c in cols if c != "Z_PK"]


def _build_chat_map(
    conn: sqlite3.Connection,
    report: "MergeReport",
    entity_map: dict[str, tuple[int, int]],
) -> None:
    cols = column_intersection(conn, "ZWACHATSESSION")
    if "ZCONTACTJID" not in cols:
        raise ValueError("ZWACHATSESSION lacks ZCONTACTJID; unrecognized iOS schema")
    insert_cols = _z_cols(cols)

    conn.execute(
        "CREATE TEMP TABLE chat_map (src_pk INTEGER PRIMARY KEY, dst_pk INTEGER NOT NULL)"
    )

    # Build select with Z_ENT remap and ZLASTMESSAGE nulled (re-pointed in
    # the post-merge denormalization pass).
    select = _select_with_overrides(
        cols=insert_cols,
        alias="s",
        overrides=_chat_overrides(insert_cols, entity_map),
    )

    sql = (
        f'INSERT INTO main.ZWACHATSESSION ({quote_cols(insert_cols)}) '
        f'{select} FROM src.ZWACHATSESSION s '
        "WHERE s.ZCONTACTJID IS NOT NULL "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM main.ZWACHATSESSION m WHERE m.ZCONTACTJID = s.ZCONTACTJID"
        ")"
    )
    cur = conn.execute(sql)
    report.chats_inserted = cur.rowcount

    conn.execute(
        "INSERT INTO chat_map (src_pk, dst_pk) "
        "SELECT s.Z_PK, m.Z_PK FROM src.ZWACHATSESSION s "
        "JOIN main.ZWACHATSESSION m ON m.ZCONTACTJID = s.ZCONTACTJID"
    )


def _chat_overrides(
    cols: list[str],
    entity_map: dict[str, tuple[int, int]],
) -> dict[str, str]:
    """Per-column SQL overrides for ZWACHATSESSION insert."""
    out: dict[str, str] = {}
    if "Z_ENT" in cols and "WAChatSession" in entity_map:
        _, dst = entity_map["WAChatSession"]
        out["Z_ENT"] = f"{dst}"
    if "ZLASTMESSAGE" in cols:
        out["ZLASTMESSAGE"] = "NULL"
    return out


def _build_groupmember_map(
    conn: sqlite3.Connection,
    report: "MergeReport",
    entity_map: dict[str, tuple[int, int]],
) -> None:
    if not (
        has_table(conn, "ZWAGROUPMEMBER", "main")
        and has_table(conn, "ZWAGROUPMEMBER", "src")
    ):
        return
    cols = column_intersection(conn, "ZWAGROUPMEMBER")
    needed = {"ZCHATSESSION", "ZMEMBERJID"}
    if not needed <= set(cols):
        report.warnings.append(
            f"ZWAGROUPMEMBER missing {needed - set(cols)}; skipping group members"
        )
        return
    insert_cols = _z_cols(cols)

    conn.execute(
        "CREATE TEMP TABLE groupmember_map (src_pk INTEGER PRIMARY KEY, dst_pk INTEGER NOT NULL)"
    )

    overrides = {"ZCHATSESSION": "cm.dst_pk"}
    if "Z_ENT" in insert_cols and "WAGroupMember" in entity_map:
        _, dst = entity_map["WAGroupMember"]
        overrides["Z_ENT"] = f"{dst}"
    select = _select_with_overrides(insert_cols, "s", overrides)
    sql = (
        f'INSERT INTO main.ZWAGROUPMEMBER ({quote_cols(insert_cols)}) '
        f'{select} FROM src.ZWAGROUPMEMBER s '
        "JOIN chat_map cm ON cm.src_pk = s.ZCHATSESSION "
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM main.ZWAGROUPMEMBER m "
        "  WHERE m.ZCHATSESSION = cm.dst_pk AND m.ZMEMBERJID = s.ZMEMBERJID"
        ")"
    )
    cur = conn.execute(sql)
    report.satellite_rows_inserted["ZWAGROUPMEMBER"] = cur.rowcount

    conn.execute(
        "INSERT INTO groupmember_map (src_pk, dst_pk) "
        "SELECT s.Z_PK, m.Z_PK FROM src.ZWAGROUPMEMBER s "
        "JOIN chat_map cm ON cm.src_pk = s.ZCHATSESSION "
        "JOIN main.ZWAGROUPMEMBER m ON m.ZCHATSESSION = cm.dst_pk "
        "  AND m.ZMEMBERJID = s.ZMEMBERJID"
    )


def _build_message_map(
    conn: sqlite3.Connection,
    report: "MergeReport",
    entity_map: dict[str, tuple[int, int]],
) -> None:
    cols = column_intersection(conn, "ZWAMESSAGE")
    required = {"ZCHATSESSION", "ZISFROMME", "ZSTANZAID"}
    missing = required - set(cols)
    if missing:
        raise ValueError(f"ZWAMESSAGE missing required columns: {missing}")
    insert_cols = _z_cols(cols)

    conn.execute(
        "CREATE TEMP TABLE message_map (src_pk INTEGER PRIMARY KEY, dst_pk INTEGER NOT NULL)"
    )

    overrides = {
        "ZCHATSESSION": "cm.dst_pk",
        # Inverse to media is set in a second pass.
        "ZMEDIAITEM": "NULL",
    }
    if "ZGROUPMEMBER" in insert_cols:
        overrides["ZGROUPMEMBER"] = (
            "(SELECT gm.dst_pk FROM groupmember_map gm WHERE gm.src_pk = s.ZGROUPMEMBER)"
        )
    # Self-references (quotes) are remapped in a second pass.
    for self_ref in ("ZPARENTMESSAGE", "ZQUOTEDMESSAGE"):
        if self_ref in insert_cols:
            overrides[self_ref] = "NULL"
    if "Z_ENT" in insert_cols and "WAMessage" in entity_map:
        _, dst = entity_map["WAMessage"]
        overrides["Z_ENT"] = f"{dst}"

    has_groupmember = has_table(conn, "ZWAGROUPMEMBER", "main") and has_table(
        conn, "ZWAGROUPMEMBER", "src"
    )
    has_groupmember_join = has_groupmember and "ZGROUPMEMBER" in insert_cols
    # `groupmember_map` only exists if _build_groupmember_map ran
    if not has_groupmember:
        overrides.pop("ZGROUPMEMBER", None)

    select = _select_with_overrides(insert_cols, "s", overrides)

    sql = (
        f'INSERT INTO main.ZWAMESSAGE ({quote_cols(insert_cols)}) '
        f'{select} FROM src.ZWAMESSAGE s '
        "JOIN chat_map cm ON cm.src_pk = s.ZCHATSESSION "
        # Use ZSTANZAID as the natural key. Some system messages have NULL
        # ZSTANZAID; we always insert those (treated as distinct).
        "WHERE s.ZSTANZAID IS NULL OR NOT EXISTS ("
        "  SELECT 1 FROM main.ZWAMESSAGE m "
        "  WHERE m.ZCHATSESSION = cm.dst_pk "
        "    AND m.ZISFROMME = s.ZISFROMME "
        "    AND m.ZSTANZAID = s.ZSTANZAID"
        ")"
    )
    cur = conn.execute(sql)
    report.messages_inserted = cur.rowcount

    conn.execute(
        "INSERT INTO message_map (src_pk, dst_pk) "
        "SELECT s.Z_PK, m.Z_PK FROM src.ZWAMESSAGE s "
        "JOIN chat_map cm ON cm.src_pk = s.ZCHATSESSION "
        "JOIN main.ZWAMESSAGE m ON m.ZCHATSESSION = cm.dst_pk "
        "  AND m.ZISFROMME = s.ZISFROMME "
        "  AND m.ZSTANZAID IS NOT NULL "
        "  AND m.ZSTANZAID = s.ZSTANZAID"
    )

    total_mapped = conn.execute("SELECT COUNT(*) FROM message_map").fetchone()[0]
    report.messages_skipped_duplicate = max(0, total_mapped - report.messages_inserted)

    # Self-reference second pass.
    for self_ref in ("ZPARENTMESSAGE", "ZQUOTEDMESSAGE"):
        if self_ref in insert_cols:
            conn.execute(
                f"UPDATE main.ZWAMESSAGE SET {self_ref} = ("
                f"  SELECT mp_q.dst_pk FROM src.ZWAMESSAGE s "
                f"  JOIN message_map mp_src ON mp_src.src_pk = s.Z_PK "
                f"  JOIN message_map mp_q ON mp_q.src_pk = s.{self_ref} "
                f"  WHERE mp_src.dst_pk = main.ZWAMESSAGE.Z_PK"
                ") "
                f"WHERE Z_PK IN (SELECT mp.dst_pk FROM message_map mp "
                f"               JOIN src.ZWAMESSAGE s ON s.Z_PK = mp.src_pk "
                f"               WHERE s.{self_ref} IS NOT NULL)"
            )

    _ = has_groupmember_join  # silence linters; flag retained for clarity


def _build_media_map(
    conn: sqlite3.Connection,
    report: "MergeReport",
    entity_map: dict[str, tuple[int, int]],
) -> None:
    if not (
        has_table(conn, "ZWAMEDIAITEM", "main")
        and has_table(conn, "ZWAMEDIAITEM", "src")
    ):
        return
    cols = column_intersection(conn, "ZWAMEDIAITEM")
    if "ZMESSAGE" not in cols:
        report.warnings.append("ZWAMEDIAITEM lacks ZMESSAGE; skipping media")
        return
    insert_cols = _z_cols(cols)

    conn.execute(
        "CREATE TEMP TABLE media_map (src_pk INTEGER PRIMARY KEY, dst_pk INTEGER NOT NULL)"
    )

    overrides: dict[str, str] = {"ZMESSAGE": "mm.dst_pk"}
    if "Z_ENT" in insert_cols and "WAMediaItem" in entity_map:
        _, dst = entity_map["WAMediaItem"]
        overrides["Z_ENT"] = f"{dst}"

    select = _select_with_overrides(insert_cols, "s", overrides)

    sql = (
        f'INSERT INTO main.ZWAMEDIAITEM ({quote_cols(insert_cols)}) '
        f'{select} FROM src.ZWAMEDIAITEM s '
        "JOIN message_map mm ON mm.src_pk = s.ZMESSAGE "
        # 1-to-1 with message: skip rows whose mapped message already has media.
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM main.ZWAMEDIAITEM x WHERE x.ZMESSAGE = mm.dst_pk"
        ")"
    )
    cur = conn.execute(sql)
    report.satellite_rows_inserted["ZWAMEDIAITEM"] = cur.rowcount

    conn.execute(
        "INSERT INTO media_map (src_pk, dst_pk) "
        "SELECT s.Z_PK, m.Z_PK FROM src.ZWAMEDIAITEM s "
        "JOIN message_map mm ON mm.src_pk = s.ZMESSAGE "
        "JOIN main.ZWAMEDIAITEM m ON m.ZMESSAGE = mm.dst_pk"
    )


def _fix_message_to_media_inverse(conn: sqlite3.Connection) -> None:
    """Set ``ZWAMESSAGE.ZMEDIAITEM`` to mirror ``ZWAMEDIAITEM.ZMESSAGE``.

    Core Data persists both sides of the 1-to-1; without this the message
    "knows" it has no media even though the row exists.
    """
    if "ZMEDIAITEM" not in list_columns(conn, "ZWAMESSAGE"):
        return
    conn.execute(
        "UPDATE main.ZWAMESSAGE SET ZMEDIAITEM = ("
        "  SELECT mi.Z_PK FROM main.ZWAMEDIAITEM mi WHERE mi.ZMESSAGE = main.ZWAMESSAGE.Z_PK"
        ") WHERE EXISTS ("
        "  SELECT 1 FROM main.ZWAMEDIAITEM mi WHERE mi.ZMESSAGE = main.ZWAMESSAGE.Z_PK"
        ")"
    )


def _copy_other_satellites(
    conn: sqlite3.Connection,
    report: "MergeReport",
    entity_map: dict[str, tuple[int, int]],
) -> None:
    """Discover and copy any other Z* satellite table referencing ZWAMESSAGE.

    These are tables like ZWAMESSAGEINFO, ZWAVCARDMENTION, ZWAGROUPEVENT —
    one-to-one or one-to-many with a message, varying by WhatsApp version.
    We dynamically detect and copy them rather than hard-coding the list.
    """
    rows = conn.execute(
        "SELECT m.name FROM main.sqlite_master m "
        "JOIN src.sqlite_master s ON s.name = m.name "
        "WHERE m.type='table' AND s.type='table' AND m.name LIKE 'Z%' "
        "  AND m.name NOT LIKE 'Z\\_%' ESCAPE '\\'"
    ).fetchall()
    handled = {"ZWACHATSESSION", "ZWAMESSAGE", "ZWAMEDIAITEM", "ZWAGROUPMEMBER"}
    for (table,) in rows:
        if table in handled or table in _CORE_DATA_INTERNAL:
            continue
        cols = column_intersection(conn, table)
        if "ZMESSAGE" not in cols:
            continue
        # Generic remap: ZMESSAGE via message_map; any other known FK column
        # via its own map if available.
        insert_cols = _z_cols(cols)
        overrides: dict[str, str] = {"ZMESSAGE": "mm.dst_pk"}
        joins = ["JOIN message_map mm ON mm.src_pk = s.ZMESSAGE"]
        if "ZCHATSESSION" in insert_cols:
            overrides["ZCHATSESSION"] = (
                "(SELECT cm.dst_pk FROM chat_map cm WHERE cm.src_pk = s.ZCHATSESSION)"
            )
        if "Z_ENT" in insert_cols:
            entity_name = _entity_name_for_table(conn, table)
            if entity_name and entity_name in entity_map:
                _, dst = entity_map[entity_name]
                overrides["Z_ENT"] = f"{dst}"

        select = _select_with_overrides(insert_cols, "s", overrides)
        sql = (
            f'INSERT OR IGNORE INTO main."{table}" ({quote_cols(insert_cols)}) '
            f'{select} FROM src."{table}" s '
            f'{" ".join(joins)}'
        )
        try:
            cur = conn.execute(sql)
            report.satellite_rows_inserted[table] = cur.rowcount
        except sqlite3.OperationalError as e:
            report.warnings.append(f"satellite {table}: {e}")


def _recompute_chatsession_denorm(conn: sqlite3.Connection) -> None:
    """Recompute per-chat denormalized counters from actual message rows.

    These show up in WhatsApp's chat list (last preview, unread badge). After
    inserting new B messages into chats already in A the cached values are
    stale.
    """
    cols = list_columns(conn, "ZWACHATSESSION")
    pieces: list[str] = []
    if "ZLASTMESSAGE" in cols:
        pieces.append(
            "ZLASTMESSAGE = ("
            "  SELECT m.Z_PK FROM main.ZWAMESSAGE m "
            "  WHERE m.ZCHATSESSION = main.ZWACHATSESSION.Z_PK "
            "  ORDER BY m.ZMESSAGEDATE DESC, m.Z_PK DESC LIMIT 1"
            ")"
        )
    if "ZLASTMESSAGEDATE" in cols:
        pieces.append(
            "ZLASTMESSAGEDATE = ("
            "  SELECT MAX(m.ZMESSAGEDATE) FROM main.ZWAMESSAGE m "
            "  WHERE m.ZCHATSESSION = main.ZWACHATSESSION.Z_PK"
            ")"
        )
    if "ZMESSAGECOUNTER" in cols:
        pieces.append(
            "ZMESSAGECOUNTER = ("
            "  SELECT COUNT(*) FROM main.ZWAMESSAGE m "
            "  WHERE m.ZCHATSESSION = main.ZWACHATSESSION.Z_PK"
            ")"
        )
    if not pieces:
        return
    conn.execute(f"UPDATE main.ZWACHATSESSION SET {', '.join(pieces)}")


def _bump_z_primarykey(conn: sqlite3.Connection, report: "MergeReport") -> None:
    """For every entity table with rows we may have inserted, bump Z_MAX.

    This is the single most failure-prone step if omitted: Core Data will
    happily allocate ``Z_PK`` values that already exist, corrupting the
    store on the next save.
    """
    rows = conn.execute(
        "SELECT name FROM main.sqlite_master "
        "WHERE type='table' AND name LIKE 'Z%' AND name NOT LIKE 'Z\\_%' ESCAPE '\\'"
    ).fetchall()
    for (table,) in rows:
        try:
            actual_max = conn.execute(
                f'SELECT COALESCE(MAX(Z_PK), 0) FROM main."{table}"'
            ).fetchone()[0]
        except sqlite3.OperationalError:
            continue
        entity_name = _entity_name_for_table(conn, table)
        if entity_name is None:
            continue
        prior = conn.execute(
            "SELECT Z_MAX FROM main.Z_PRIMARYKEY WHERE Z_NAME = ?", (entity_name,)
        ).fetchone()
        if prior is None:
            continue
        prior_max = prior[0] or 0
        if actual_max > prior_max:
            conn.execute(
                "UPDATE main.Z_PRIMARYKEY SET Z_MAX = ? WHERE Z_NAME = ?",
                (actual_max, entity_name),
            )
            report.ios_z_max_bumps[entity_name] = (prior_max, actual_max)


def _verify_integrity(conn: sqlite3.Connection, report: "MergeReport") -> None:
    """Cheap sanity checks. Failures append to report; merge still commits."""
    # 1. Z_MAX >= MAX(Z_PK) for every entity
    rows = conn.execute(
        "SELECT name FROM main.sqlite_master "
        "WHERE type='table' AND name LIKE 'Z%' AND name NOT LIKE 'Z\\_%' ESCAPE '\\'"
    ).fetchall()
    for (table,) in rows:
        try:
            actual_max = conn.execute(
                f'SELECT COALESCE(MAX(Z_PK), 0) FROM main."{table}"'
            ).fetchone()[0]
        except sqlite3.OperationalError:
            continue
        entity_name = _entity_name_for_table(conn, table)
        if entity_name is None:
            continue
        prior = conn.execute(
            "SELECT Z_MAX FROM main.Z_PRIMARYKEY WHERE Z_NAME = ?", (entity_name,)
        ).fetchone()
        if prior and (prior[0] or 0) < actual_max:
            report.ios_integrity_failures.append(
                f"Z_PRIMARYKEY.Z_MAX for {entity_name} ({prior[0]}) "
                f"is below actual MAX(Z_PK) ({actual_max})"
            )

    # 2. ZWAMEDIAITEM.ZMESSAGE points to an existing message
    bad_media = conn.execute(
        "SELECT COUNT(*) FROM main.ZWAMEDIAITEM mi "
        "WHERE mi.ZMESSAGE IS NOT NULL "
        "  AND NOT EXISTS (SELECT 1 FROM main.ZWAMESSAGE m WHERE m.Z_PK = mi.ZMESSAGE)"
    ).fetchone()[0]
    if bad_media:
        report.ios_integrity_failures.append(
            f"{bad_media} ZWAMEDIAITEM rows reference a missing ZWAMESSAGE"
        )

    # 3. ZWAMESSAGE.ZCHATSESSION points to an existing chat
    bad_chat = conn.execute(
        "SELECT COUNT(*) FROM main.ZWAMESSAGE m "
        "WHERE m.ZCHATSESSION IS NOT NULL "
        "  AND NOT EXISTS (SELECT 1 FROM main.ZWACHATSESSION c WHERE c.Z_PK = m.ZCHATSESSION)"
    ).fetchone()[0]
    if bad_chat:
        report.ios_integrity_failures.append(
            f"{bad_chat} ZWAMESSAGE rows reference a missing ZWACHATSESSION"
        )


# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------


def _select_with_overrides(
    cols: list[str],
    alias: str,
    overrides: dict[str, str],
) -> str:
    """Build a ``SELECT`` clause for ``cols`` from table ``alias``.

    Columns in ``overrides`` use the override expression instead of
    ``alias.col``. Always returns just ``"SELECT a, b, c"`` (no FROM).
    """
    parts: list[str] = []
    for c in cols:
        if c in overrides:
            parts.append(f'{overrides[c]} AS "{c}"')
        else:
            parts.append(f'{alias}."{c}"')
    return "SELECT " + ", ".join(parts)
