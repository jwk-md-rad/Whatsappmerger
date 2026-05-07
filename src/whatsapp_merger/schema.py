"""Schema introspection for WhatsApp msgstore.db files.

WhatsApp adds and renames columns frequently, so the merger never assumes a
fixed column set. Instead, for every table it intersects the column lists of
both source and destination and operates only on the common subset.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Iterable


MODERN_MARKER = "message"
LEGACY_MARKER = "messages"


@dataclass(frozen=True)
class SchemaFlavor:
    """Which generation of msgstore.db we're looking at."""

    name: str  # "modern" | "legacy"
    message_table: str
    chat_table: str | None  # legacy has no chat table
    jid_table: str | None  # legacy has no jid table


def detect_flavor(conn: sqlite3.Connection, schema: str = "main") -> SchemaFlavor:
    tables = {
        row[0]
        for row in conn.execute(
            f"SELECT name FROM \"{schema}\".sqlite_master WHERE type='table'"
        )
    }
    if {"ZWAMESSAGE", "ZWACHATSESSION", "Z_PRIMARYKEY"} <= tables:
        return SchemaFlavor("ios", "ZWAMESSAGE", "ZWACHATSESSION", None)
    if MODERN_MARKER in tables and "chat" in tables and "jid" in tables:
        return SchemaFlavor("modern", "message", "chat", "jid")
    if LEGACY_MARKER in tables:
        return SchemaFlavor("legacy", "messages", None, None)
    raise ValueError(
        "Database does not look like a WhatsApp backup "
        f"(tables seen in {schema!r}: {sorted(tables)[:10]}...)"
    )


def list_columns(conn: sqlite3.Connection, table: str, schema: str = "main") -> list[str]:
    rows = conn.execute(f'PRAGMA "{schema}".table_info("{table}")').fetchall()
    return [r[1] for r in rows]


def column_intersection(
    conn: sqlite3.Connection,
    table: str,
    main_schema: str = "main",
    src_schema: str = "src",
) -> list[str]:
    main_cols = list_columns(conn, table, main_schema)
    src_cols = set(list_columns(conn, table, src_schema))
    # preserve declaration order from main
    return [c for c in main_cols if c in src_cols]


def has_table(conn: sqlite3.Connection, table: str, schema: str = "main") -> bool:
    row = conn.execute(
        f"SELECT 1 FROM \"{schema}\".sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def message_satellite_tables(conn: sqlite3.Connection, schema: str = "main") -> list[str]:
    """Tables that hang off the message table by ``message_row_id``.

    We discover them dynamically rather than hard-coding the list, since
    WhatsApp adds new ones over time.
    """
    rows = conn.execute(
        f"SELECT name FROM \"{schema}\".sqlite_master "
        "WHERE type='table' AND name LIKE 'message\\_%' ESCAPE '\\'"
    ).fetchall()
    out: list[str] = []
    for (name,) in rows:
        cols = list_columns(conn, name, schema)
        if "message_row_id" in cols:
            out.append(name)
    return out


def quote_cols(cols: Iterable[str]) -> str:
    return ", ".join(f'"{c}"' for c in cols)
