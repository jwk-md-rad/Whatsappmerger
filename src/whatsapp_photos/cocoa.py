"""Apple Cocoa epoch helpers.

Core Data stores timestamps as seconds since 2001-01-01 UTC, not the Unix
epoch. The offset is exactly 978307200 seconds.
"""
from __future__ import annotations

COCOA_EPOCH_OFFSET = 978_307_200


def cocoa_to_unix(seconds: float | int | None) -> int | None:
    if seconds is None:
        return None
    return int(seconds + COCOA_EPOCH_OFFSET)


def unix_to_cocoa(seconds: float | int | None) -> float | None:
    if seconds is None:
        return None
    return float(seconds - COCOA_EPOCH_OFFSET)
