"""Parsing helpers: bulk credential input and imapsync console output."""

from __future__ import annotations

import re

# ---------------------------------------------------------------- credentials

_DELIMITED = ("\t", ";")
_LOOSE = (",", ":", " ")


class CredentialError(ValueError):
    pass


def parse_credentials(text: str) -> tuple[list[dict], list[str]]:
    """Parse bulk credential input.

    Accepted per line::

        user@example.com;secret
        user@example.com;secret;login-name
        user@example.com,secret
        user@example.com secret
        user@example.com:secret

    The first delimiter found decides the split, so passwords may contain any
    of the other characters. With ``;``/tab a third field overrides the IMAP
    login name (for servers where it differs from the address).
    """
    entries: list[dict] = []
    errors: list[str] = []
    seen: set[str] = set()

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        email = password = login = ""
        for delim in _DELIMITED:
            if delim in line:
                parts = [p.strip() for p in line.split(delim)]
                email = parts[0]
                if len(parts) == 2:
                    password = parts[1]
                elif len(parts) == 3:
                    password, login = parts[1], parts[2]
                else:
                    password = delim.join(parts[1:])
                break
        else:
            for delim in _LOOSE:
                head, sep, tail = line.partition(delim)
                if sep:
                    email, password = head.strip(), tail.strip()
                    break

        if not email or not password:
            errors.append(f"line {lineno}: could not split address and password")
            continue
        if "@" not in email:
            errors.append(f"line {lineno}: {email!r} does not look like an address")
            continue

        key = email.lower()
        if key in seen:
            errors.append(f"line {lineno}: duplicate entry for {email}")
            continue
        seen.add(key)

        entries.append(
            {"email": email, "password": password, "login": login or email}
        )

    return entries, errors


def parse_list(text: str) -> list[str]:
    """Split a textarea holding one pattern per line (commas also allowed)."""
    items: list[str] = []
    for chunk in re.split(r"[\r\n]+", text or ""):
        for part in chunk.split(","):
            part = part.strip()
            if part:
                items.append(part)
    return items


# ------------------------------------------------------------ imapsync output

# "Folder       3/12 INBOX.Sent                     -> INBOX.Sent"
_FOLDER_RE = re.compile(r"^Folder\s+(\d+)/(\d+)\s+(.+?)\s+->\s+(.+?)\s*$")
_ETA_RE = re.compile(r"ETA[:\s].*?\b(\d+)/(\d+)\s+msgs?\s+left", re.IGNORECASE)
_STATS_START_RE = re.compile(r"^\+{2,}\s*Statistics", re.IGNORECASE)
_STATS_LINE_RE = re.compile(r"^([A-Za-z][^:]{0,60}?)\s*:\s*(.*\S)\s*$")
_DETECTED_RE = re.compile(r"^Detected\s+(\d+)\s+error", re.IGNORECASE)
_COPIED_RE = re.compile(r"\bcopied to\b", re.IGNORECASE)
# imapsync reports problems as "Err 1/3: ..." and plain "Error: ..." lines.
_ERROR_RE = re.compile(r"^(?:Err\s+\d+/\d+:|Error:|Failure:|Fatal)", re.IGNORECASE)


class OutputParser:
    """Turns the imapsync log stream into progress + final statistics."""

    def __init__(self) -> None:
        self.progress: dict = {
            "folder": None,
            "folder_index": 0,
            "folder_total": 0,
            "messages_left": 0,
            "messages_total": 0,
            "copied": 0,
            "last_line": "",
        }
        self.stats: dict[str, str] = {}
        self.errors: list[str] = []
        self._in_stats = False

    def feed(self, line: str) -> None:
        line = line.rstrip("\r\n")
        if not line.strip():
            return
        self.progress["last_line"] = line[:400]

        match = _FOLDER_RE.match(line)
        if match:
            self.progress["folder_index"] = int(match.group(1))
            self.progress["folder_total"] = int(match.group(2))
            # imapsync prints folder names bracketed: "[INBOX.Sent]".
            self.progress["folder"] = match.group(3).strip().strip("[]")
            self.progress["messages_left"] = 0
            self.progress["messages_total"] = 0
            return

        if _COPIED_RE.search(line):
            self.progress["copied"] += 1

        # The per-message lines carry the ETA, so this must come last.
        match = _ETA_RE.search(line)
        if match:
            self.progress["messages_left"] = int(match.group(1))
            self.progress["messages_total"] = int(match.group(2))
            return

        if _STATS_START_RE.match(line):
            self._in_stats = True
            return

        match = _DETECTED_RE.search(line)
        if match:
            self.stats["Detected errors"] = match.group(1)

        if self._in_stats:
            match = _STATS_LINE_RE.match(line)
            if match:
                self.stats[match.group(1).strip()] = match.group(2).strip()
        elif _ERROR_RE.match(line) and len(self.errors) < 20:
            self.errors.append(line[:400])

    def summary_error(self) -> str:
        if self.errors:
            return self.errors[-1]
        return ""
