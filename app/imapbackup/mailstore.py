"""Inspection of the local Maildir backup store."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from . import config

_SAFE = re.compile(r"[^a-z0-9._+@-]+")


def sanitize_local_user(email: str) -> str:
    """Turn an address into a filesystem/IMAP friendly local user name."""
    name = _SAFE.sub("_", email.strip().lower()).strip("._")
    return name or "mailbox"


def maildir_path(local_user: str) -> Path:
    return config.MAILDIR_ROOT / local_user / "Maildir"


def _count(folder: Path) -> tuple[int, int]:
    messages = 0
    size = 0
    for sub in ("cur", "new"):
        path = folder / sub
        if not path.is_dir():
            continue
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    try:
                        if entry.is_file(follow_symlinks=False):
                            messages += 1
                            size += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return messages, size


def scan(local_user: str) -> dict:
    """Return per-folder message counts and sizes for one backed up mailbox.

    Blocking filesystem work - call it through a thread.
    """
    root = maildir_path(local_user)
    report = {"exists": root.is_dir(), "bytes": 0, "messages": 0, "folders": []}
    if not report["exists"]:
        return report

    messages, size = _count(root)
    folders = [{"name": "INBOX", "messages": messages, "bytes": size}]

    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if not entry.name.startswith(".") or not entry.is_dir(
                    follow_symlinks=False
                ):
                    continue
                # Maildir++ encodes the hierarchy as ".Parent.Child".
                name = entry.name[1:].replace(".", "/")
                if not name:
                    continue
                sub_messages, sub_size = _count(Path(entry.path))
                folders.append(
                    {"name": name, "messages": sub_messages, "bytes": sub_size}
                )
    except OSError:
        pass

    folders.sort(key=lambda f: (f["name"] != "INBOX", f["name"].lower()))
    report["folders"] = folders
    report["messages"] = sum(f["messages"] for f in folders)
    report["bytes"] = sum(f["bytes"] for f in folders)
    return report


def delete(local_user: str) -> None:
    target = config.MAILDIR_ROOT / local_user
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)


def disk_usage() -> dict:
    try:
        usage = shutil.disk_usage(config.MAILDIR_ROOT)
        return {"total": usage.total, "used": usage.used, "free": usage.free}
    except OSError:
        return {"total": 0, "used": 0, "free": 0}
