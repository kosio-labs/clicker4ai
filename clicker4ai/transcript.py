"""Cheap facts read from the end of a claude transcript (JSONL): how big the
context was at the last turn, which model ran it, and the name the user
gave the session (/rename in the terminal or Rename in the app).

The context size is what resuming the session costs to rebuild in the
prompt cache, so it is shown even for sessions with no live process.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

HEAD_BYTES = 64 * 1024
TAIL_START = 256 * 1024
TAIL_MAX = 16 * 1024 * 1024   # tool results can bury the last usage row

# (path) -> ((size, mtime_ns), stats)
_cache: dict[str, tuple[tuple[int, int], dict]] = {}


def _usage_tokens(row: dict) -> tuple[int, str | None] | None:
    """Context size of one assistant row: everything the next request
    sends again (input + cache reads/writes + this reply)."""
    if row.get("type") != "assistant" or row.get("isSidechain"):
        return None
    msg = row.get("message") or {}
    u = msg.get("usage") or {}
    total = sum(int(u.get(k) or 0) for k in (
        "input_tokens", "cache_creation_input_tokens",
        "cache_read_input_tokens", "output_tokens"))
    if total <= 0:   # synthetic error rows carry zero usage
        return None
    model = msg.get("model")
    return total, (model if model and not model.startswith("<") else None)


def _scan(lines: list[bytes], want_usage: bool, want_title: bool) -> tuple:
    usage = title = None
    for line in reversed(lines):
        if want_title and title is None and b'"custom-title"' in line:
            try:
                title = json.loads(line).get("customTitle") or None
            except (ValueError, AttributeError):
                pass
        if want_usage and usage is None and b'"usage"' in line:
            try:
                usage = _usage_tokens(json.loads(line))
            except (ValueError, AttributeError, TypeError):
                pass
        if (usage or not want_usage) and (title or not want_title):
            break
    return usage, title


def _read(path: Path, size: int) -> dict:
    usage = title = None
    with open(path, "rb") as f:
        span = TAIL_START
        while True:
            start = max(0, size - span)
            f.seek(start)
            lines = f.read(size - start).split(b"\n")
            if start > 0:
                lines = lines[1:]   # partial first line
            u, t = _scan(lines, usage is None, title is None)
            usage, title = usage or u, title or t
            # the title is re-appended near the tail by the CLI (and the
            # head covers the rest); only the usage row is worth reading
            # further back for
            if usage or start == 0 or span >= TAIL_MAX:
                break
            span *= 4
        if title is None and start > 0:
            f.seek(0)
            _, title = _scan(f.read(HEAD_BYTES).split(b"\n")[:-1], False, True)
    return {"context_tokens": usage[0] if usage else None,
            "model": usage[1] if usage else None,
            "custom_title": title}


def transcript_stats(path: Path | None) -> dict:
    """{"context_tokens", "model", "custom_title"} — each None when unknown.
    Cached per file until it changes size or mtime."""
    empty = {"context_tokens": None, "model": None, "custom_title": None}
    if path is None:
        return empty
    try:
        st = os.stat(path)
    except OSError:
        return empty
    key = (st.st_size, st.st_mtime_ns)
    hit = _cache.get(str(path))
    if hit and hit[0] == key:
        return hit[1]
    try:
        stats = _read(path, st.st_size)
    except OSError:
        return empty
    _cache[str(path)] = (key, stats)
    return stats


# (path) -> ((size, mtime_ns), ms)
_ts_cache: dict[str, tuple[tuple[int, int], int]] = {}


def last_activity_ms(path: Path | None) -> int | None:
    """Time of the last message (user or assistant row) in a transcript, in
    ms; the file's mtime when the tail has none. Not the mtime itself: the
    CLI appends bookkeeping rows (cost-state, last-prompt) when its process
    exits, so a server restart or a stopped runner would count as activity.
    Reads the last TAIL_START bytes; cached per file until it changes."""
    if path is None:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (st.st_size, st.st_mtime_ns)
    hit = _ts_cache.get(str(path))
    if hit and hit[0] == key:
        return hit[1]
    ms = None
    try:
        with open(path, "rb") as f:
            start = max(0, st.st_size - TAIL_START)
            f.seek(start)
            lines = f.read().split(b"\n")
        if start > 0:
            lines = lines[1:]   # partial first line
        for line in reversed(lines):
            if b'"timestamp"' not in line:
                continue
            try:
                row = json.loads(line)
                if row.get("type") in ("user", "assistant") and row.get("timestamp"):
                    ms = int(datetime.fromisoformat(row["timestamp"]).timestamp() * 1000)
                    break
            except (ValueError, AttributeError, TypeError):
                continue
    except OSError:
        return None
    if ms is None:
        ms = st.st_mtime_ns // 1_000_000
    _ts_cache[str(path)] = (key, ms)
    return ms


def short_path(p: str | None) -> str:
    """Home directory as ~ — the cards have little room."""
    if not p:
        return ""
    home = str(Path.home())
    if p == home or p.startswith(home + "/"):
        return "~" + p[len(home):]
    return p
