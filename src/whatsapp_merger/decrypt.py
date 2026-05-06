"""Optional decryption wrapper around ``wa-crypt-tools``.

We do not reimplement the cryptography. ``wa-crypt-tools`` is well-maintained
and tracks WhatsApp's format updates; we just adapt it to a uniform
``decrypt_to_path`` entrypoint and degrade gracefully when it isn't installed.
"""
from __future__ import annotations

import os
from pathlib import Path


CRYPT_SUFFIXES = (".crypt12", ".crypt14", ".crypt15")


def looks_encrypted(path: os.PathLike[str] | str) -> bool:
    return str(path).lower().endswith(CRYPT_SUFFIXES)


def decrypt_to_path(
    encrypted: os.PathLike[str] | str,
    key: os.PathLike[str] | str,
    out_path: os.PathLike[str] | str,
) -> Path:
    """Decrypt ``encrypted`` using ``key`` and write plaintext to ``out_path``.

    ``key`` may be a path to a key file (e.g. ``key`` for crypt14 or
    ``encrypted_backup.key`` for crypt15) or a string holding the 64-character
    hex-encoded key. Returns the output path.
    """
    try:
        from wa_crypt_tools.lib.db.dbfactory import DatabaseFactory  # type: ignore
        from wa_crypt_tools.lib.key.keyfactory import KeyFactory  # type: ignore
    except ImportError as e:  # pragma: no cover - depends on env
        raise RuntimeError(
            "wa-crypt-tools is not installed. Either install it "
            "(`pip install whatsapp-merger[crypto]`) or pass already-decrypted "
            "msgstore.db files to the merger."
        ) from e

    encrypted = Path(encrypted)
    out_path = Path(out_path)

    key_obj = KeyFactory.new(str(key))
    db, header = DatabaseFactory.from_file(encrypted)
    plaintext = db.decrypt(key_obj, encrypted.read_bytes())
    out_path.write_bytes(plaintext)
    return out_path
