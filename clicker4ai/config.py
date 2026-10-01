"""Server configuration: paths, private file helpers, LAN IP detection."""

from __future__ import annotations

import contextlib
import json
import os
import socket
import stat
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


def _data_dir() -> Path:
    """~/.clicker4ai, also when running from a source checkout: a data/
    folder inside the repo would sit under an allowed root (the repo usually
    lives in one) and hand devices the tokens and other sessions' events.
    C4AI_DATA_DIR overrides it (tests)."""
    env = os.environ.get("C4AI_DATA_DIR")
    return Path(env or Path.home() / ".clicker4ai").expanduser().resolve()


DATA_DIR = _data_dir()
SESSIONS_DIR = DATA_DIR / "sessions"
# CLI debug files (`--debug-file`), written only at log_level "debug"
LOGS_DIR = DATA_DIR / "logs"
CONFIG_PATH = DATA_DIR / "config.json"

DEFAULT_PORT = 8780
DEFAULT_HOST = "127.0.0.1"
# How many sessions may hold a live `claude` process at once. Each one costs
# roughly 300 MB of RAM, while stopping one costs the price of rebuilding its
# context on the next message — so this is a memory ceiling, not a policy.
DEFAULT_MAX_RUNNERS = 6
# A device with a passkey is locked after this many minutes without a
# request (0 = never); the passkey wakes it. See auth.DeviceStore.verify.
DEFAULT_LOCK_IDLE_MINUTES = 15
LOG_LEVELS = ("error", "warning", "info", "debug")
DEFAULT_LOG_LEVEL = "warning"


class ConfigError(ValueError):
    """config.json holds something the server cannot run with; cli.main
    prints the message alone, without a traceback."""

# Incognito chats (sessions.py): each gets a fresh folder
# INCOGNITO_DIR/<device id>/<sid>, erased together with everything Claude Code
# kept for it after INCOGNITO_IDLE_HOURS without activity or when its device
# goes. Inside DATA_DIR, so no allowed root can hold it (see PROTECTED_DIRS).
# C4AI_INCOGNITO_DIR moves it (tests).
INCOGNITO_DIR = Path(os.environ.get("C4AI_INCOGNITO_DIR")
                     or DATA_DIR / "incognito").expanduser().resolve()
INCOGNITO_IDLE_HOURS = 24
# Claude Code's own directory (transcripts of every session, incognito ones
# included, settings, credentials); CLAUDE_CONFIG_DIR moves it.
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR")
                  or Path.home() / ".claude").expanduser().resolve()
# the global config (MCP servers, projects) sits beside ~/.claude, but inside
# CLAUDE_CONFIG_DIR when that is set
CLAUDE_JSON = (CLAUDE_DIR / ".claude.json" if os.environ.get("CLAUDE_CONFIG_DIR")
               else Path.home() / ".claude.json")
# Never reachable from a device, even when an allowed root contains one
# (scope.py, projects.browse), and never an allowed root or inside one
# (_parse_roots). A device's own incognito folder is the one exception.
PROTECTED_DIRS = (DATA_DIR, INCOGNITO_DIR, CLAUDE_DIR)


def _tighten(path: Path, mode: int) -> int | None:
    """chmod `path` to `mode`; the old mode when it was looser than that,
    else None (a new path never is: it is created with `mode` or tighter)."""
    was = stat.S_IMODE(os.stat(path).st_mode)
    if was != mode:
        os.chmod(path, mode)
    return was if was & ~mode else None


def _warn_loose(what: str) -> None:
    """One stderr line (a terminal, or pm2's error log under pm2): someone
    or something opened up the data, and the fix alone would hide that."""
    print(f"clicker4ai: {what}", file=sys.stderr)


def secure_dir(path: Path) -> Path:
    """Create a directory (if missing) and restrict it to the owner (0700)."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (was := _tighten(path, 0o700)) is not None:
        _warn_loose(f"{path} was {was:04o}, now 0700")
    return path


def write_private(path: Path, data: dict | list | str) -> None:
    """Atomically write an owner-only (0600) file: temp file in the same
    directory, fsync, rename over the target. Dicts/lists become JSON."""
    text = data if isinstance(data, str) else json.dumps(data, indent=2)
    secure_dir(path.parent)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:   # mkstemp → 0600
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def open_private_append(path: Path):
    """Open a file for appending text, creating it 0600 if missing."""
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    return os.fdopen(fd, "a", encoding="utf-8")


def _harden_existing() -> None:
    """Tighten data written before files were created private (umask 0002
    → 0775 dirs / 0664 files). Only our own paths: DATA_DIR may be any
    directory (C4AI_DATA_DIR), so never walk it blindly."""
    paths: list[tuple[Path, int]] = [(DATA_DIR, 0o700), (SESSIONS_DIR, 0o700)]
    paths += [(DATA_DIR / n, 0o600) for n in
              ("config.json", "devices.json", "pairing.json", "passkeys.json",
               ".auth.lock")]
    with contextlib.suppress(OSError):
        for d in SESSIONS_DIR.iterdir():
            if d.is_dir() and not d.is_symlink():
                paths += [(d, 0o700), (d / "meta.json", 0o600),
                          (d / "events.jsonl", 0o600)]
    loose = []
    for p, mode in paths:
        with contextlib.suppress(OSError):
            if p.exists() and not p.is_symlink():
                if (was := _tighten(p, mode)) is not None:
                    loose.append(f"{p} was {was:04o}, now {mode:04o}")
    if loose:
        more = f" (and {len(loose) - 1} more)" if len(loose) > 1 else ""
        _warn_loose(loose[0] + more)


def _parse_host(raw) -> str:
    if raw is None:
        return DEFAULT_HOST
    if not isinstance(raw, str) or not raw.strip() or any(c.isspace() for c in raw.strip()):
        raise ConfigError("config.json: host must be an IP address or a host name")
    return raw.strip()


def _parse_port(raw) -> int:
    if raw is None:
        return DEFAULT_PORT
    if isinstance(raw, bool) or (isinstance(raw, float) and not raw.is_integer()):
        raise ConfigError("config.json: port must be a whole number")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ConfigError("config.json: port must be a number")
    if not 1 <= value <= 65535:
        raise ConfigError("config.json: port must be between 1 and 65535")
    return value


def _parse_max_runners(raw) -> int:
    if raw is None:
        return DEFAULT_MAX_RUNNERS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ConfigError("config.json: max_runners must be a positive integer")
    if value < 1:
        raise ConfigError("config.json: max_runners must be at least 1")
    return value


def _parse_lock_idle(raw) -> int:
    if raw is None:
        return DEFAULT_LOCK_IDLE_MINUTES
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ConfigError("config.json: lock_idle_minutes must be a whole number")
    if value < 0:
        raise ConfigError("config.json: lock_idle_minutes must be 0 (off) or more")
    return value


def _parse_log_level(raw) -> str:
    if raw is None:
        return DEFAULT_LOG_LEVEL
    if not isinstance(raw, str) or raw.lower() not in LOG_LEVELS:
        raise ConfigError("config.json: log_level must be one of "
                         + ", ".join(LOG_LEVELS))
    return raw.lower()


def _parse_roots(raw) -> list[str]:
    """config.json "allowed_roots": non-empty list of directories
    (~ expanded), required. The home directory and its ancestors are refused:
    a root there would hand every device ~/.ssh and ~/.claude.json (MCP
    tokens) through browse and the session tools. So is a root inside
    PROTECTED_DIRS; a root that contains one is fine (scope.py hides it)."""
    if raw is None:
        raise ConfigError("No folders set: run `c4ai config roots add ~/work` "
                          "(the folders devices may use)")
    if not isinstance(raw, list) or not raw \
            or not all(isinstance(r, str) and r.strip() for r in raw):
        raise ConfigError("config.json: allowed_roots must be a non-empty list of paths")
    roots = [Path(r).expanduser().resolve() for r in raw]
    home = Path.home().resolve()
    for r in roots:
        if home.is_relative_to(r):
            raise ConfigError(f"config.json: allowed_roots must not contain {r} "
                             "(the home directory or above); use sub-folders, "
                             'e.g. ["~/work"]')
        for d in PROTECTED_DIRS:
            if r.is_relative_to(d):
                raise ConfigError(f"config.json: allowed_roots must not contain {r}: "
                                 f"it lies in {d}, which no device may reach")
    return [str(r) for r in roots]


@dataclass
class Config:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    # Global ceiling for what remote devices can reach (see scope.py);
    # absolute, resolved paths.
    allowed_roots: list[str] = field(default_factory=list)
    max_runners: int = DEFAULT_MAX_RUNNERS
    extra: dict = field(default_factory=dict)

    @classmethod
    def load(cls) -> "Config":
        secure_dir(DATA_DIR)
        secure_dir(SESSIONS_DIR)
        _harden_existing()
        if CONFIG_PATH.exists():
            try:
                raw = json.loads(CONFIG_PATH.read_text())
            except ValueError as e:
                raise ConfigError(f"{CONFIG_PATH} is not valid JSON ({e}); "
                                  "fix it by hand") from None
            if not isinstance(raw, dict):
                raise ConfigError(f"{CONFIG_PATH} must hold a JSON object; "
                                  "fix it by hand")
        else:
            raw = {}
        # The long-lived master token is gone (login = one-time pairing codes);
        # drop it from configs written by older versions.
        changed = raw.pop("token", None) is not None
        cfg = cls(
            host=_parse_host(raw.get("host")),
            port=_parse_port(raw.get("port")),
            allowed_roots=_parse_roots(raw.get("allowed_roots")),
            max_runners=_parse_max_runners(raw.get("max_runners")),
            extra={k: v for k, v in raw.items()
                   if k not in ("host", "port", "allowed_roots", "max_runners")},
        )
        if changed:
            cfg.save()
        return cfg

    @property
    def lock_idle_minutes(self) -> int:
        """config.json "lock_idle_minutes" — kept in `extra`, so a config
        that never set it is not rewritten with the default."""
        return _parse_lock_idle(self.extra.get("lock_idle_minutes"))

    @property
    def log_level(self) -> str:
        """config.json "log_level": what the server writes to stderr (pm2's
        error log). warning = stalls and processes that died; info = plus
        every start, stop and turn; debug = plus every SDK event, the
        `claude` process's stderr and its own debug file in DATA_DIR/logs/."""
        return _parse_log_level(self.extra.get("log_level"))

    def save(self) -> None:
        payload = {
            "host": self.host,
            "port": self.port,
            "allowed_roots": self.allowed_roots,
            "max_runners": self.max_runners,
            **self.extra,
        }
        write_private(CONFIG_PATH, payload)


def lan_ip() -> str | None:
    """Best-effort LAN IP (no traffic is actually sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.168.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return None

