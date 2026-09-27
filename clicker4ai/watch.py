"""Live transcript tailing: mirror turns appended to a claude session's
JSONL by OTHER processes (a terminal `claude` on the same session) into
the UI event log, so the webapp shows terminal activity in real time.

Dedup contract (enforced by the caller in sessions.py): the tailer only
reads while this UI session's own subprocess is quiet — rows our own
turns write are skipped by fast-forwarding the read offset to EOF when
the turn's result arrives.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from pathlib import Path

from .config import CLAUDE_DIR
from .history import _assistant_events, _user_events

try:
    # Private SDK helper (may move between SDK versions): resolves the transcript path
    # with the CLI's exact directory munging, worktree and long-path rules.
    from claude_agent_sdk._internal.sessions import _resolve_session_file_path
except ImportError:  # pragma: no cover — SDK internals moved
    _resolve_session_file_path = None


# Claude session ids are UUIDs; anything else could escape the projects dir
# (the id becomes the `<id>.jsonl` file name).
SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")


def valid_session_id(session_id: str) -> bool:
    return isinstance(session_id, str) and SESSION_ID_RE.fullmatch(session_id) is not None


def transcript_path(session_id: str, cwd: str | None) -> Path | None:
    if not valid_session_id(session_id):
        return None
    if _resolve_session_file_path is not None:
        try:
            return _resolve_session_file_path(session_id, cwd)
        except Exception:
            pass
    if not cwd:
        return None
    folder = project_dir(cwd)
    p = folder / f"{session_id}.jsonl" if folder is not None else None
    return p if p is not None and p.exists() else None


# the CLI cuts longer folder names and appends a hash
PROJECT_DIR_NAME_MAX = 200


def project_dir(cwd: str) -> Path | None:
    """The CLI's transcript folder for `cwd`, for when the SDK's private
    helpers are gone. A long name is matched by its first 200 characters:
    the hash differs between runtimes (the SDK does the same)."""
    projects = CLAUDE_DIR / "projects"
    safe = re.sub(r"[^a-zA-Z0-9]", "-",
                  unicodedata.normalize("NFC", os.path.realpath(cwd)))
    if len(safe) <= PROJECT_DIR_NAME_MAX:
        return projects / safe
    prefix = safe[:PROJECT_DIR_NAME_MAX] + "-"
    try:
        for entry in projects.iterdir():
            if entry.is_dir() and entry.name.startswith(prefix):
                return entry
    except OSError:
        pass
    return None


def rows_to_events(lines: list[str]) -> list[dict]:
    """Raw transcript JSONL lines → UI events, marked external.

    Applies the same visibility rules as the SDK's chain reader: only
    top-level user/assistant rows, no meta/sidechain/team entries.
    """
    evs: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(row, dict) or row.get("type") not in ("user", "assistant"):
            continue
        if row.get("isMeta") or row.get("isSidechain") or row.get("teamName"):
            continue
        message = row.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if content is None:
            continue
        if row["type"] == "user":
            new = _user_events(content)
        else:
            new = _assistant_events(content)
        for ev in new:
            ev["external"] = True
        evs.extend(new)
    return evs
