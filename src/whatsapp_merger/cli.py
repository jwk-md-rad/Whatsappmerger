"""Command-line entry point for the WhatsApp backup merger."""
from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
from pathlib import Path

from . import __version__
from .decrypt import decrypt_to_path, looks_encrypted
from .merger import merge_databases


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="whatsapp-merger",
        description="Merge two WhatsApp Android msgstore.db backups.",
    )
    p.add_argument("db_a", help="First backup (kept as the merge base)")
    p.add_argument("db_b", help="Second backup (merged into A)")
    p.add_argument("-o", "--output", required=True, help="Output msgstore.db path")
    p.add_argument(
        "--key-a",
        help="Decryption key file or 64-char hex key for db_a (if encrypted)",
    )
    p.add_argument(
        "--key-b",
        help="Decryption key file or 64-char hex key for db_b (if encrypted)",
    )
    p.add_argument(
        "--prefer-newer",
        action="store_true",
        help=(
            "Where the same message exists in both DBs, replace dest fields "
            "with source fields when the source has a newer received_timestamp "
            "(Android only)"
        ),
    )
    p.add_argument(
        "--allow-model-hash-mismatch",
        action="store_true",
        help=(
            "iOS only: proceed even if the two ChatStorage.sqlite files come "
            "from different WhatsApp Core Data model versions. The destination "
            "(db_a) must be the newer version's file."
        ),
    )
    p.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging"
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def _maybe_decrypt(path: str, key: str | None, tmpdir: Path, label: str) -> Path:
    if not looks_encrypted(path):
        return Path(path)
    if not key:
        raise SystemExit(
            f"{label} ({path}) appears encrypted but no --key-{label.lower()} was given."
        )
    out = tmpdir / f"{label}.decrypted.db"
    print(f"Decrypting {label}: {path}", file=sys.stderr)
    return decrypt_to_path(path, key, out)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    with tempfile.TemporaryDirectory(prefix="wamerger-") as td:
        tmp = Path(td)
        plain_a = _maybe_decrypt(args.db_a, args.key_a, tmp, "A")
        plain_b = _maybe_decrypt(args.db_b, args.key_b, tmp, "B")

        report = merge_databases(
            plain_a,
            plain_b,
            args.output,
            prefer_newer=args.prefer_newer,
            allow_model_hash_mismatch=args.allow_model_hash_mismatch,
        )

    print(f"Merged successfully to {args.output}")
    print(f"  Schema flavor: {report.flavor}")
    if report.flavor != "ios":
        print(f"  JIDs added:           {report.jids_inserted}")
    print(f"  Chats added:          {report.chats_inserted}")
    print(f"  Messages added:       {report.messages_inserted}")
    print(f"  Messages skipped (dup): {report.messages_skipped_duplicate}")
    if report.satellite_rows_inserted:
        print("  Satellite tables:")
        for t, n in sorted(report.satellite_rows_inserted.items()):
            print(f"    {t}: {n}")
    if report.flavor == "ios":
        match = report.ios_model_hashes_match
        print(f"  Core Data model hashes match: {match}")
        if report.ios_entity_id_remaps:
            print("  Z_ENT remaps (src -> dst):")
            for name, (s, d) in sorted(report.ios_entity_id_remaps.items()):
                print(f"    {name}: {s} -> {d}")
        if report.ios_z_max_bumps:
            print("  Z_PRIMARYKEY.Z_MAX bumps:")
            for name, (old, new) in sorted(report.ios_z_max_bumps.items()):
                print(f"    {name}: {old} -> {new}")
        if report.ios_integrity_failures:
            print("  Integrity failures (REVIEW BEFORE INJECTING):")
            for f in report.ios_integrity_failures:
                print(f"    - {f}")
        else:
            print("  Integrity checks: OK")
    if report.warnings:
        print("  Warnings:")
        for w in report.warnings:
            print(f"    - {w}")
    print(f"  Elapsed: {report.elapsed_seconds:.2f}s")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
