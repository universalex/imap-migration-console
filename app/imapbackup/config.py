"""Runtime configuration, read from environment variables."""

from __future__ import annotations

import os
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
LOG_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "imapbackup.db"

# Root of the local Maildir store, shared with the dovecot container.
MAILDIR_ROOT = Path(os.environ.get("MAILDIR_ROOT", "/srv/mail"))

# Local IMAP server that holds the backups.
DOVECOT_HOST = os.environ.get("DOVECOT_HOST", "dovecot")
DOVECOT_PORT = _int("DOVECOT_PORT", 143)
DOVECOT_MASTER_PASSWORD = os.environ.get("DOVECOT_MASTER_PASSWORD", "")

IMAPSYNC_BIN = os.environ.get("IMAPSYNC_BIN", "/usr/local/bin/imapsync")
MAX_CONCURRENT_JOBS = max(1, _int("MAX_CONCURRENT_JOBS", 2))
IMAP_TIMEOUT = _int("IMAP_TIMEOUT", 180)

# Optional HTTP basic auth for the web interface.
AUTH_USER = os.environ.get("AUTH_USER", "")
AUTH_PASSWORD = os.environ.get("AUTH_PASSWORD", "")

# Used to encrypt stored mailbox passwords. When empty a key file is generated
# inside DATA_DIR so credentials survive restarts.
SECRET_KEY = os.environ.get("SECRET_KEY", "")

# How often the background task refreshes mailbox sizes (seconds).
RESCAN_INTERVAL = _int("RESCAN_INTERVAL", 300)

VERSION = "1.0.0"
