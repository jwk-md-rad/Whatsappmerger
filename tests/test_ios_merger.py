from __future__ import annotations

import sqlite3
from pathlib import Path

from whatsapp_merger.merger import merge_databases

from .fixtures import IOS_ENTITIES_A, make_ios_db


def _q(db: Path, sql: str, params: tuple = ()) -> list[tuple]:
    c = sqlite3.connect(db)
    try:
        return c.execute(sql, params).fetchall()
    finally:
        c.close()


def test_ios_merge_basic_dedup_and_remap(tmp_path: Path) -> None:
    a = tmp_path / "A.sqlite"
    b = tmp_path / "B.sqlite"
    out = tmp_path / "merged.sqlite"
    make_ios_db(a, dataset="A")
    make_ios_db(b, dataset="B")

    report = merge_databases(a, b, out, allow_model_hash_mismatch=True)

    assert report.flavor == "ios"
    # B contributes: 1 new chat (Carol), 2 new messages (STANZA_B2, STANZA_C1)
    assert report.chats_inserted == 1
    assert report.messages_inserted == 2
    assert report.messages_skipped_duplicate == 1

    # Entity-id remap was needed because A and B used different Z_ENT ids.
    assert "WAMessage" in report.ios_entity_id_remaps
    src, dst = report.ios_entity_id_remaps["WAMessage"]
    assert dst == IOS_ENTITIES_A["WAMessage"]
    assert src != dst

    # Inserted messages bear the destination's Z_ENT (not B's).
    rows = _q(
        out,
        "SELECT ZSTANZAID, Z_ENT FROM ZWAMESSAGE WHERE ZSTANZAID IN ('STANZA_B2','STANZA_C1') "
        "ORDER BY ZSTANZAID",
    )
    assert rows == [
        ("STANZA_B2", IOS_ENTITIES_A["WAMessage"]),
        ("STANZA_C1", IOS_ENTITIES_A["WAMessage"]),
    ]


def test_ios_merge_z_primarykey_bumped(tmp_path: Path) -> None:
    a = tmp_path / "A.sqlite"
    b = tmp_path / "B.sqlite"
    out = tmp_path / "merged.sqlite"
    make_ios_db(a, dataset="A")
    make_ios_db(b, dataset="B")
    report = merge_databases(a, b, out, allow_model_hash_mismatch=True)

    # Z_MAX must equal MAX(Z_PK) in every entity table.
    rows = _q(out, "SELECT Z_NAME, Z_MAX FROM Z_PRIMARYKEY ORDER BY Z_NAME")
    z_max = dict(rows)
    actual = {
        "WAChatSession": _q(out, "SELECT MAX(Z_PK) FROM ZWACHATSESSION")[0][0],
        "WAMessage": _q(out, "SELECT MAX(Z_PK) FROM ZWAMESSAGE")[0][0],
        "WAMediaItem": _q(out, "SELECT MAX(Z_PK) FROM ZWAMEDIAITEM")[0][0],
        "WAMessageInfo": _q(out, "SELECT MAX(Z_PK) FROM ZWAMESSAGEINFO")[0][0],
    }
    for name, max_pk in actual.items():
        assert z_max[name] == max_pk, f"Z_MAX off for {name}: {z_max[name]} vs {max_pk}"

    assert report.ios_integrity_failures == []


def test_ios_merge_inverse_relationship_set(tmp_path: Path) -> None:
    a = tmp_path / "A.sqlite"
    b = tmp_path / "B.sqlite"
    out = tmp_path / "merged.sqlite"
    make_ios_db(a, dataset="A")
    make_ios_db(b, dataset="B")
    merge_databases(a, b, out, allow_model_hash_mismatch=True)

    # Carol's media: ZWAMESSAGE.ZMEDIAITEM must mirror ZWAMEDIAITEM.ZMESSAGE.
    rows = _q(
        out,
        "SELECT m.ZTEXT, mi.ZMEDIALOCALPATH FROM ZWAMESSAGE m "
        "JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM "
        "WHERE m.ZSTANZAID = 'STANZA_C1'",
    )
    assert rows == [("From Carol (B)", "/Library/Media/carol.jpg")]


def test_ios_merge_satellite_remapped(tmp_path: Path) -> None:
    a = tmp_path / "A.sqlite"
    b = tmp_path / "B.sqlite"
    out = tmp_path / "merged.sqlite"
    make_ios_db(a, dataset="A")
    make_ios_db(b, dataset="B")
    merge_databases(a, b, out, allow_model_hash_mismatch=True)

    rows = _q(
        out,
        "SELECT m.ZSTANZAID, mi.ZRECEIPTINFO FROM ZWAMESSAGEINFO mi "
        "JOIN ZWAMESSAGE m ON m.Z_PK = mi.ZMESSAGE ORDER BY m.ZSTANZAID",
    )
    # A's receipt for STANZA_A1 + B's receipt for STANZA_B2.
    assert (b"receipt-A1",) in [r[1:] for r in rows if r[0] == "STANZA_A1"]
    assert (b"receipt-B2",) in [r[1:] for r in rows if r[0] == "STANZA_B2"]


def test_ios_chatsession_denormalized_recomputed(tmp_path: Path) -> None:
    a = tmp_path / "A.sqlite"
    b = tmp_path / "B.sqlite"
    out = tmp_path / "merged.sqlite"
    make_ios_db(a, dataset="A")
    make_ios_db(b, dataset="B")
    merge_databases(a, b, out, allow_model_hash_mismatch=True)

    # Bob's chat now has 3 messages (A's STANZA_B1 + B's STANZA_B2 = 2 distinct
    # messages from B's contribution; A originally had STANZA_B1 only, so total = 2).
    bob = _q(
        out,
        "SELECT ZMESSAGECOUNTER, ZLASTMESSAGEDATE FROM ZWACHATSESSION WHERE ZCONTACTJID='222@s.whatsapp.net'",
    )[0]
    assert bob[0] == 2
    assert bob[1] == 700_000_400.0  # STANZA_B2 is the newest in Bob's chat

    # ZLASTMESSAGE points at the newest message in the chat.
    last_text = _q(
        out,
        "SELECT m.ZTEXT FROM ZWACHATSESSION c JOIN ZWAMESSAGE m ON m.Z_PK = c.ZLASTMESSAGE "
        "WHERE c.ZCONTACTJID='222@s.whatsapp.net'",
    )
    assert last_text == [("New from me to Bob (B)",)]


def test_ios_idempotent(tmp_path: Path) -> None:
    a = tmp_path / "A.sqlite"
    b = tmp_path / "B.sqlite"
    once = tmp_path / "once.sqlite"
    twice = tmp_path / "twice.sqlite"
    make_ios_db(a, dataset="A")
    make_ios_db(b, dataset="B")
    merge_databases(a, b, once, allow_model_hash_mismatch=True)
    second = merge_databases(once, b, twice, allow_model_hash_mismatch=True)

    assert second.messages_inserted == 0
    assert second.chats_inserted == 0


def test_ios_model_hash_match_when_identical(tmp_path: Path) -> None:
    a = tmp_path / "A.sqlite"
    b = tmp_path / "B.sqlite"
    out = tmp_path / "merged.sqlite"
    same = b"<plist-shared>"
    make_ios_db(a, dataset="A", model_plist=same)
    make_ios_db(b, dataset="B", model_plist=same)
    report = merge_databases(a, b, out, allow_model_hash_mismatch=False)
    assert report.ios_model_hashes_match is True


def test_ios_warning_on_model_hash_mismatch(tmp_path: Path) -> None:
    a = tmp_path / "A.sqlite"
    b = tmp_path / "B.sqlite"
    out = tmp_path / "merged.sqlite"
    make_ios_db(a, dataset="A", model_plist=b"<plist-A>")
    make_ios_db(b, dataset="B", model_plist=b"<plist-B>")
    report = merge_databases(a, b, out, allow_model_hash_mismatch=True)
    assert report.ios_model_hashes_match is False
    assert any("model hashes differ" in w for w in report.warnings)
