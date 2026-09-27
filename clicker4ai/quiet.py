"""Quiet hours: a nightly window in which no device may start a new turn.

config.json "quiet_hours": "HH:MM-HH:MM" (e.g. "00:30-08:00"; a window may
cross midnight), in "quiet_tz" (an IANA zone such as "Europe/London") when
set, else in the host's local time. Never the device's zone: a phone could
move the window by changing its own clock. Set with `c4ai quiet-hours`,
never from the app — it is a limit you set for yourself, so a phone cannot
lift it. A device is left out only by `--quiet-exempt` (on `pair` or
`devices set <device>`).

A turn already running when the window opens finishes, and its permission
cards can still be answered; what is refused is the next prompt. The chat
checks in main.py; True View (the real TUI, where the server sees only
keystrokes) gets a UserPromptSubmit hook (quiet_hook.py) that asks the same
question for the device that last typed into the terminal.

Read from config.json on every check (cached by mtime), so a change made
with the CLI applies without a restart — also to terminals already open.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import CONFIG_PATH, SESSIONS_DIR

_WINDOW_RE = re.compile(r"(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})")

# True View: present while the device that last typed into the session's
# terminal is exempt (term_attach.py writes it, quiet_hook.py reads it)
TERM_EXEMPT_FILE = "term_quiet_exempt"

_cache: tuple = (None, None, None)   # (config stamp, window, zone)


def parse_window(raw: str) -> tuple[int, int]:
    """"HH:MM-HH:MM" → (start, end) in minutes after midnight."""
    m = _WINDOW_RE.fullmatch(str(raw).replace(" ", ""))
    if not m:
        raise ValueError("quiet hours must look like 00:30-08:00")
    h1, m1, h2, m2 = map(int, m.groups())
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        raise ValueError("quiet hours: hours 0–23, minutes 0–59")
    start, end = h1 * 60 + m1, h2 * 60 + m2
    if start == end:
        raise ValueError("quiet hours: start and end must differ")
    return start, end


def parse_zone(raw: str) -> ZoneInfo:
    """An IANA zone name → ZoneInfo."""
    try:
        return ZoneInfo(str(raw))
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"unknown time zone {raw!r} (e.g. Europe/London)") from None


def fmt(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _load() -> tuple[tuple[int, int] | None, ZoneInfo | None]:
    """(window, zone) from config.json. A window the CLI would refuse
    (edited by hand) counts as none; such a zone as host time, so a typo
    does not lift the limit."""
    global _cache
    try:
        st = CONFIG_PATH.stat()
        stamp = (st.st_mtime_ns, st.st_ino)
    except OSError:
        return None, None
    if _cache[0] == stamp:
        return _cache[1], _cache[2]
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
        raw = cfg.get("quiet_hours")
        value = parse_window(raw) if raw else None
    except (OSError, ValueError, AttributeError):
        cfg, value = {}, None
    try:
        zone = parse_zone(cfg["quiet_tz"]) if cfg.get("quiet_tz") else None
    except (ValueError, AttributeError):
        zone = None
    _cache = (stamp, value, zone)
    return value, zone


def window() -> tuple[int, int] | None:
    """The configured window, or None when there is none."""
    return _load()[0]


def active(now: datetime | None = None) -> tuple[int, int] | None:
    """The window when it is open now, else None."""
    win, zone = _load()
    if win is None:
        return None
    now = now or datetime.now(zone)
    t = now.hour * 60 + now.minute
    start, end = win
    inside = start <= t < end if start < end else (t >= start or t < end)
    return win if inside else None


def refusal(device: dict | None) -> str | None:
    """Why this device may not start a turn now, or None."""
    win = active()
    if win is None or (device and device.get("quiet_exempt")):
        return None
    return (f"Quiet hours until {fmt(win[1])} — a running turn finishes, "
            "new prompts wait")


def mark_term_writer(sid: str, exempt: bool) -> None:
    """Record whether the device typing into True View is exempt."""
    path = SESSIONS_DIR / sid / TERM_EXEMPT_FILE
    try:
        if exempt:
            path.touch(mode=0o600)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass
