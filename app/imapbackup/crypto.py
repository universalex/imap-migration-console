"""Symmetric encryption for stored mailbox passwords."""

from __future__ import annotations

import base64
import hashlib
import os
import stat

from cryptography.fernet import Fernet, InvalidToken

from . import config

_fernet: Fernet | None = None


def _key() -> bytes:
    if config.SECRET_KEY:
        digest = hashlib.sha256(config.SECRET_KEY.encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest)

    key_file = config.DATA_DIR / "secret.key"
    if key_file.exists():
        return key_file.read_bytes().strip()

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    key_file.write_bytes(key)
    os.chmod(key_file, stat.S_IRUSR | stat.S_IWUSR)
    return key


def _cipher() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_key())
    return _fernet


def encrypt(value: str) -> str:
    return _cipher().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(value: str) -> str:
    if not value:
        return ""
    try:
        return _cipher().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:  # wrong SECRET_KEY / corrupted row
        raise RuntimeError(
            "Stored password could not be decrypted - SECRET_KEY changed?"
        ) from exc
