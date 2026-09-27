"""Directory scopes: which parts of the filesystem a device may reach.

`allowed_roots` in config.json is the global ceiling (required;
never the home directory or above, see config.py). A device may carry its own `roots` — a subset of the
ceiling, set only from the CLI (`pair --root`, `devices set`). A
device with no `roots` key at all gets the whole ceiling; an *empty* list
is the opposite and means "nothing yet", which is what a passkey login
creates (passkey.py) until the CLI gives it folders. Own roots that fall
outside the ceiling (e.g. after the ceiling was narrowed) are dropped; if
none remain the device sees nothing — it never widens to the ceiling.

A scope limits what the remote UI can see and control. It is not a
sandbox: a Claude session may still touch files outside its cwd, which is
why bypassPermissions/dontAsk are not available remotely (sessions.py).

A device that has at least one folder also owns a private directory,
INCOGNITO_DIR/<device id>, for its incognito chat. It counts for access
checks (`contains`, `resolve`) but not for `roots`, so it never shows up in
the folder picker and no other device's scope includes it.

config.PROTECTED_DIRS (this server's data, the incognito folders, Claude
Code's own directory) are outside every scope, even under a root.
"""

from __future__ import annotations

from pathlib import Path

from .config import INCOGNITO_DIR, PROTECTED_DIRS


class ScopeError(ValueError):
    pass


def resolve_dir(raw: str) -> Path:
    return Path(raw).expanduser().resolve()


def _within(p: Path, roots: tuple[Path, ...] | list[Path]) -> bool:
    return any(p.is_relative_to(r) for r in roots)


class Scope:
    def __init__(self, roots: list[Path], private: list[Path] | None = None):
        self.roots = tuple(roots)
        self.private = tuple(private or ())

    def resolve(self, raw: str | Path | None) -> Path | None:
        """Resolved path if it lies inside the scope, else None."""
        if not raw:
            return None
        try:
            p = Path(raw).expanduser().resolve()
        except (OSError, RuntimeError):
            return None
        if p.is_relative_to(INCOGNITO_DIR):
            # a root may still contain it (C4AI_INCOGNITO_DIR can move it), but
            # only the owning device reaches its incognito folder
            return p if _within(p, self.private) else None
        if _within(p, PROTECTED_DIRS):
            return None
        return p if _within(p, self.roots) else None

    def contains(self, raw: str | Path | None) -> bool:
        return self.resolve(raw) is not None

    def root_of(self, p: Path) -> Path | None:
        """Deepest scope root containing an already-resolved path."""
        hits = [r for r in self.roots if p.is_relative_to(r)]
        return max(hits, key=lambda r: len(r.parts)) if hits else None


def incognito_home(device_id: str) -> Path | None:
    """The device's private directory for incognito chats; None for an id
    that could not be a single safe path component."""
    if not isinstance(device_id, str) or not device_id.isalnum():
        return None
    return INCOGNITO_DIR / device_id


def device_scope(allowed_roots: list[str], device: dict | None) -> Scope:
    ceiling = [resolve_dir(r) for r in allowed_roots]
    own = device.get("roots") if device else None
    if own is None:
        roots = ceiling
    elif not isinstance(own, list) or not own:
        return Scope([])          # explicit empty: no folders at all
    else:
        roots = [p for p in (resolve_dir(r) for r in own if isinstance(r, str))
                 if _within(p, ceiling)]
    home = incognito_home(device.get("id")) if device and roots else None
    return Scope(roots, [home] if home else None)


def validate_roots(allowed_roots: list[str], raws: list[str]) -> list[str]:
    """CLI input → absolute, existing directories inside the ceiling."""
    ceiling = [resolve_dir(r) for r in allowed_roots]
    out: list[str] = []
    for raw in raws:
        p = resolve_dir(raw)
        if not p.is_dir():
            raise ScopeError(f"not a directory: {raw}")
        if not _within(p, ceiling):
            raise ScopeError(f"outside allowed_roots ({', '.join(allowed_roots)}): {raw}")
        if str(p) not in out:
            out.append(str(p))
    return out
