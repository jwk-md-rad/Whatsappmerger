"""Lazy thumbnail generation, cached on disk.

We don't precompute thumbnails at ingest time — the user might have tens
of thousands of photos and they'd pay the cost up front. Instead, we
generate on first request to ``/api/thumb/<id>`` and cache to a
sibling directory of the database.
"""
from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image, ImageOps


log = logging.getLogger("whatsapp_photos.thumbs")


def thumb_path(cache_dir: Path, photo_id: int, size: int) -> Path:
    return cache_dir / f"{photo_id}_{size}.jpg"


def ensure_thumb(
    source: Path,
    cache_dir: Path,
    photo_id: int,
    size: int = 240,
) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = thumb_path(cache_dir, photo_id, size)
    if out.exists() and out.stat().st_size > 0:
        return out
    try:
        with Image.open(source) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((size, size))
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGB")
            im.save(out, "JPEG", quality=82, optimize=True)
    except Exception as e:  # pragma: no cover - depends on PIL backend
        log.warning("thumbnail failed for %s: %s", source, e)
        raise
    return out
