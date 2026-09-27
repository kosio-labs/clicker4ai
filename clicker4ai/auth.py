"""Per-device sessions and one-time pairing codes.

Login flow: a one-time pairing code (printed by the server at start or by
`c4ai pair`) is redeemed once → the server creates a device
session and sets its random token as a cookie. There is no long-lived
master token.

Storage (data dir is 0700, files 0600, written atomically):
  devices.json  {"devices": [{id, name, token_sha256, created_at, last_seen,
                              roots?, manage_devices, terminal, passkey,
                              require_passkey, locked, quiet_exempt, files,
                              files_upload}]}
  pairing.json  {"codes":   [{code_sha256, expires_at, roots?, manage_devices,
                              terminal, require_passkey, quiet_exempt, files,
                              files_upload}]}

A pairing code carries the grants chosen when it was issued (CLI only):
`roots` limits the device to those directories (see scope.py; absent = the
whole allowed_roots), `manage_devices` lets it list and revoke other
devices in the app and `terminal` allows True View (the real Claude Code
TUI — effectively a shell, so off by default). All can be changed later
from the CLI.

Device names are unique (case-folded): a second iPhone is stored as
"iPhone 2", and renaming onto a taken name is refused. A name may not be
another device's id either, so the CLI can take either one (`resolve`).

A device paired with `require_passkey` (the default for pairing codes and
passkey logins) may do nothing but register a passkey until it has one —
see `needs_passkey`; turning the requirement off is left to the CLI.

A device can be *locked*: the cookie stays in the browser and the record
keeps its grants, but every request is refused until a passkey unlocks it
(passkey.py). Getting back in then needs both the cookie and the owner's
biometrics, and a locked session does not slide its idle expiry — a device
left locked still ages out after SESSION_IDLE_SECS. The server also locks
a device carrying a passkey on its own once it has not been heard from for
`idle_lock_secs` (config `lock_idle_minutes`), so an app reopened later —
or a stolen cookie — needs the passkey again.

Only SHA-256 hashes of session tokens and pairing codes are stored, so a
leaked file grants no access. Both files are shared with the CLI process
(`pair`, `devices`, `revoke`); access is serialized with an flock and the
server reloads devices.json when the file changes (mtime/inode), so a CLI
revoke takes effect on the next request. An unreadable or corrupt
devices.json (as opposed to a missing one) is not taken for "no devices":
every device is refused until the file is fixed, but nothing writes over
it and the incognito sweep holds off (DeviceStore.read_error).
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path

from .config import DATA_DIR, secure_dir, write_private

DEVICES_PATH = DATA_DIR / "devices.json"
PAIRING_PATH = DATA_DIR / "pairing.json"
LOCK_PATH = DATA_DIR / ".auth.lock"

log = logging.getLogger("clicker4ai.auth")

SESSION_IDLE_SECS = 30 * 24 * 3600   # device session expires after 30 idle days
PAIRING_TTL_SECS = 120               # one-time pairing code lifetime
LAST_SEEN_WRITE_SECS = 300           # persist last_seen at most this often
NAME_MAX = 64

# Pairing codes: 10 chars of Crockford-style base32 (no 0/1/I/L/O/U),
# shown as XXXXX-XXXXX — typable on a phone, ~50 bits, 2 min, single use.
_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
_CODE_LEN = 10


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@contextlib.contextmanager
def _locked():
    secure_dir(DATA_DIR)
    fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# passkey.py keeps its own file (passkeys.json) in the same data dir and
# under this same lock, so CLI and server never interleave writes.
file_lock = _locked


def _read_json(path: Path, key: str) -> list[dict]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    items = data.get(key) if isinstance(data, dict) else None
    return [x for x in items if isinstance(x, dict)] if isinstance(items, list) else []


# ---------------- pairing codes ----------------

def normalize_code(raw: str) -> str:
    return re.sub(r"[^0-9A-Z]", "", (raw or "").upper())


def issue_pairing_code(ttl: float = PAIRING_TTL_SECS, roots: list[str] | None = None,
                       manage_devices: bool = False, terminal: bool = False,
                       require_passkey: bool = True, quiet_exempt: bool = False,
                       files: bool = False, files_upload: bool = False) -> str:
    """Create a one-time pairing code; returns it formatted XXXXX-XXXXX.
    `roots` must already be validated (scope.validate_roots): None means
    the whole allowed_roots, a list limits it, and an empty list means no
    folders at all — which is what the CLI hands out by default.
    `files_upload` implies `files`, as in `devices set`."""
    code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LEN))
    now = time.time()
    entry = {"code_sha256": _sha256(code), "expires_at": now + ttl,
             "manage_devices": bool(manage_devices), "terminal": bool(terminal),
             "require_passkey": bool(require_passkey),
             "quiet_exempt": bool(quiet_exempt),
             "files": bool(files or files_upload), "files_upload": bool(files_upload)}
    if roots is not None:
        entry["roots"] = list(roots)
    with _locked():
        codes = [c for c in _read_json(PAIRING_PATH, "codes")
                 if c.get("expires_at", 0) > now]
        codes.append(entry)
        write_private(PAIRING_PATH, {"codes": codes})
    return f"{code[:5]}-{code[5:]}"


def redeem_pairing_code(raw: str) -> dict | None:
    """Consume a pairing code → its grants {"roots", "manage_devices",
    "terminal", "require_passkey", "quiet_exempt", "files", "files_upload"}.
    Succeeds only once, and only before the code expires; else None."""
    code = normalize_code(raw)
    if len(code) != _CODE_LEN:
        return None
    digest = _sha256(code)
    now = time.time()
    with _locked():
        codes = _read_json(PAIRING_PATH, "codes")
        live = [c for c in codes if c.get("expires_at", 0) > now]
        hit = next((c for c in live
                    if hmac.compare_digest(str(c.get("code_sha256", "")), digest)), None)
        if hit is not None:
            live.remove(hit)
        if hit is not None or len(live) != len(codes):
            write_private(PAIRING_PATH, {"codes": live})
    if hit is None:
        return None
    return {"roots": hit.get("roots"),   # None = all, [] = none
            "manage_devices": bool(hit.get("manage_devices")),
            "terminal": bool(hit.get("terminal")),
            "require_passkey": bool(hit.get("require_passkey", True)),
            "quiet_exempt": bool(hit.get("quiet_exempt")),
            "files": bool(hit.get("files")),
            "files_upload": bool(hit.get("files_upload"))}


# ---------------- device sessions ----------------

class DevicesUnreadable(RuntimeError):
    """devices.json exists but cannot be read or parsed: saving the empty
    list we fell back to would wipe every other device."""

def device_name_from_ua(ua: str) -> str:
    ua = ua or ""
    for needle, name in (("iPhone", "iPhone"), ("iPad", "iPad"),
                         ("Android", "Android"), ("Macintosh", "Mac"),
                         ("Windows", "Windows"), ("Linux", "Linux")):
        if needle in ua:
            return name
    return "Unknown device"


def _clean_name(name: str) -> str:
    name = " ".join(str(name or "").split())
    return name[:NAME_MAX]


def needs_passkey(device: dict) -> bool:
    """A device that must carry a passkey but has none yet: everything
    except registering one (and signing out) is refused."""
    return bool(device.get("require_passkey")) and not device.get("passkey")


class DeviceStore:
    """devices.json, cached in memory and reloaded when the file changes."""

    def __init__(self, path: Path = DEVICES_PATH):
        self.path = path
        # (st_mtime_ns, st_ino): writes are atomic renames, so the inode
        # changes on every save even within one mtime tick
        self._stamp: tuple[int, int] | None = None
        self._devices: list[dict] = []
        # Set by the server (0 = off, and always off in the CLI): lock a
        # device with a passkey after this long without a request.
        self.idle_lock_secs = 0
        # device id -> last request, in memory; last_seen is written only
        # every LAST_SEEN_WRITE_SECS (half a shorter idle lock) and covers
        # a server restart
        self._active: dict[str, float] = {}
        # why devices.json could not be read (None = fine or missing)
        self.read_error: str | None = None

    def _reload_if_changed(self) -> None:
        stamp = self._file_stamp()
        if stamp == self._stamp:
            return
        try:
            data = json.loads(self.path.read_text()) if stamp else {}
            items = data.get("devices", []) if isinstance(data, dict) else None
            if not isinstance(items, list):
                raise ValueError("no \"devices\" list")
        except FileNotFoundError:
            items = []   # removed between stat and read
        except (OSError, ValueError) as e:
            # refuse everyone rather than take it for "no devices"; the stamp
            # stays old, so the next call reads again (a chmod fix keeps it)
            if self.read_error is None:
                log.warning("%s unreadable, every device refused until fixed: %s",
                            self.path, e)
            self.read_error = str(e)
            self._devices = []
            return
        if self.read_error is not None:
            log.warning("%s readable again", self.path)
        self.read_error = None
        self._devices = [x for x in items if isinstance(x, dict)]
        self._stamp = stamp

    def check_readable(self) -> None:
        """Raise DevicesUnreadable before anything is spent on a write
        that would fail (a one-time pairing code)."""
        self._reload_if_changed()
        if self.read_error is not None:
            raise DevicesUnreadable(f"{self.path.name} unreadable: {self.read_error}")

    def _file_stamp(self) -> tuple[int, int] | None:
        try:
            st = self.path.stat()
        except OSError:
            return None
        return st.st_mtime_ns, st.st_ino

    def _save(self) -> None:
        if self.read_error is not None:
            raise DevicesUnreadable(f"{self.path.name} unreadable: {self.read_error}")
        write_private(self.path, {"devices": self._devices})
        self._stamp = self._file_stamp()

    def _prune(self, now: float) -> bool:
        before = len(self._devices)
        self._devices = [d for d in self._devices
                         if now - d.get("last_seen", 0) < SESSION_IDLE_SECS]
        return len(self._devices) != before

    def _taken(self) -> set[str]:
        """Every name and id in use, case-folded: a name must not read as
        another device's id, since the CLI accepts either."""
        return ({_clean_name(d.get("name", "")).casefold() for d in self._devices}
                | {str(d.get("id", "")).casefold() for d in self._devices})

    def _unique_name(self, name: str) -> str:
        """Names are unique, so two iPhones become "iPhone" and "iPhone 2"
        rather than an unreadable pair. Assumes the caller holds the lock."""
        base = _clean_name(name) or "Unknown device"
        taken = self._taken()
        if base.casefold() not in taken:
            return base
        for n in range(2, 100):
            candidate = _clean_name(f"{base} {n}")
            if candidate.casefold() not in taken:
                return candidate
        return _clean_name(f"{base} {secrets.token_hex(2)}")

    def create(self, name: str, roots: list[str] | None = None,
               manage_devices: bool = False, terminal: bool = False,
               require_passkey: bool = False, quiet_exempt: bool = False,
               files: bool = False, files_upload: bool = False) -> tuple[str, dict]:
        """New device session → (raw cookie token, device record).
        `roots` None means the whole allowed_roots, a list limits it, and
        an empty list means no folders at all (a passkey login)."""
        token = secrets.token_urlsafe(32)
        now = time.time()
        dev = {
            "id": "",            # id and name are filled under the lock,
            "name": "",          # where both are checked against every other
            "token_sha256": _sha256(token),
            "created_at": now,
            "last_seen": now,
            "manage_devices": bool(manage_devices),
            "terminal": bool(terminal),
            "require_passkey": bool(require_passkey),
            "quiet_exempt": bool(quiet_exempt),
            "files": bool(files or files_upload),
            "files_upload": bool(files_upload),
        }
        if roots is not None:
            dev["roots"] = list(roots)
        with _locked():
            self._reload_if_changed()
            self._prune(now)
            taken = self._taken()
            dev["id"] = next(i for i in iter(lambda: secrets.token_hex(4), None)
                             if i not in taken)
            dev["name"] = self._unique_name(name)
            self._devices.append(dev)
            self._save()
        return token, dev

    def verify(self, token: str | None) -> dict | None:
        """Device record for a cookie token, or None (unknown / expired).
        Slides the idle expiry forward — except for a locked device, whose
        clock keeps running so it cannot be kept alive by an app sitting
        on the lock screen. Callers must check `locked` themselves."""
        if not token:
            return None
        digest = _sha256(token)
        now = time.time()
        self._reload_if_changed()
        dev = next((d for d in self._devices
                    if hmac.compare_digest(str(d.get("token_sha256", "")), digest)), None)
        if dev is None or now - dev.get("last_seen", 0) >= SESSION_IDLE_SECS:
            return None
        if dev.get("locked"):
            return dev
        if self.idle_lock_secs and dev.get("passkey"):
            last = max(self._active.get(dev["id"], 0), dev.get("last_seen", 0))
            if now - last >= self.idle_lock_secs:
                self.set_locked(dev["id"], True)
                return {**dev, "locked": True}
        self._active[dev["id"]] = now
        # a short idle lock needs a fresher last_seen, or a restart (which
        # forgets _active) would lock a phone that was just in use
        every = min(LAST_SEEN_WRITE_SECS, self.idle_lock_secs / 2) \
            if self.idle_lock_secs else LAST_SEEN_WRITE_SECS
        if now - dev.get("last_seen", 0) >= every:
            with _locked():
                self._reload_if_changed()
                fresh = next((d for d in self._devices if d.get("id") == dev["id"]), None)
                if fresh is None:
                    return None  # revoked meanwhile
                fresh["last_seen"] = now
                self._prune(now)
                self._save()
                dev = fresh
        return dev

    def _update(self, device_id: str, fn) -> bool:
        with _locked():
            self._reload_if_changed()
            dev = next((d for d in self._devices if d.get("id") == device_id), None)
            if dev is None:
                return False
            fn(dev)
            self._save()
        return True

    def set_roots(self, device_id: str, roots: list[str] | None) -> bool:
        """Limit a device to `roots` (validated). None = whole
        allowed_roots, a list limits it, an empty list = no folders."""
        def fn(dev: dict) -> None:
            if roots is None:
                dev.pop("roots", None)
            else:
                dev["roots"] = list(roots)
        return self._update(device_id, fn)

    def set_grants(self, device_id: str, *, roots: list[str] | None | bool = False,
                   add_roots: list[str] | None = None, manage: bool | None = None,
                   terminal: bool | None = None,
                   require_passkey: bool | None = None,
                   quiet_exempt: bool | None = None, files: bool | None = None,
                   files_upload: bool | None = None) -> dict | None:
        """Change several grants in one write (the CLI's `devices set`).
        `roots` False = keep, None = whole allowed_roots, a list limits it
        ([] = no folders); `add_roots` appends instead, and is ignored on a
        device that already has every root. None elsewhere = keep. Returns
        the updated device, or None when there is no such device."""
        out: list = [None]
        def fn(dev: dict) -> None:
            if roots is None:
                dev.pop("roots", None)
            elif roots is not False:
                dev["roots"] = list(roots)
            if add_roots and dev.get("roots") is not None:
                own = dev["roots"]
                dev["roots"] = own + [r for r in add_roots if r not in own]
            for key, val in (("manage_devices", manage), ("terminal", terminal),
                             ("require_passkey", require_passkey),
                             ("quiet_exempt", quiet_exempt), ("files", files),
                             ("files_upload", files_upload)):
                if val is not None:
                    dev[key] = bool(val)
            out[0] = dict(dev)
        return out[0] if self._update(device_id, fn) else None

    def set_terminal(self, device_id: str, on: bool) -> bool:
        return self._update(device_id, lambda dev: dev.__setitem__("terminal", bool(on)))

    def name_taken(self, name: str, except_id: str | None = None) -> bool:
        """Is another device already called this, or has it as its id?
        Compared case-folded and whitespace-collapsed, so "My iPhone" and
        "my  iphone" clash."""
        wanted = _clean_name(name).casefold()
        self._reload_if_changed()
        return any((_clean_name(d.get("name", "")).casefold() == wanted
                    or str(d.get("id", "")).casefold() == wanted)
                   and d.get("id") != except_id for d in self._devices)

    def resolve(self, ref: str) -> str | None:
        """Device id for an id or a name (the CLI takes either). An exact
        id wins; names compare like name_taken."""
        self._reload_if_changed()
        if any(d.get("id") == ref for d in self._devices):
            return ref
        wanted = _clean_name(ref).casefold()
        return next((d.get("id") for d in self._devices
                     if _clean_name(d.get("name", "")).casefold() == wanted), None)

    def set_name(self, device_id: str, name: str) -> bool:
        """Rename a device. The caller checks name_taken first."""
        clean = _clean_name(name)
        if not clean:
            return False
        return self._update(device_id, lambda dev: dev.__setitem__("name", clean))

    def set_locked(self, device_id: str, on: bool) -> bool:
        """Lock (or unlock) a device session without touching its grants.
        Unlocking counts as activity, so the idle lock does not fire again
        at once — also when the CLI unlocks, which cannot reach _active."""
        def fn(dev: dict) -> None:
            dev["locked"] = bool(on)
            if not on:
                dev["last_seen"] = time.time()
        return self._update(device_id, fn)

    def set_passkey(self, device_id: str, on: bool) -> bool:
        """Mark that this device session carries a passkey — risky actions
        on it then need a fresh confirmation (passkey.needs_stepup)."""
        return self._update(device_id, lambda dev: dev.__setitem__("passkey", bool(on)))

    def set_require_passkey(self, device_id: str, on: bool) -> bool:
        return self._update(device_id,
                            lambda dev: dev.__setitem__("require_passkey", bool(on)))

    def clear_passkey(self, device_ids, still_used: set[str]) -> None:
        """After a credential is removed: devices left without any key lose
        the passkey mark, or they would keep asking for a step-up (or wait
        on an unlock) that nothing can give."""
        for device_id in device_ids:
            if device_id not in still_used:
                self.set_passkey(device_id, False)

    def revoke(self, device_id: str) -> bool:
        with _locked():
            self._reload_if_changed()
            before = len(self._devices)
            self._devices = [d for d in self._devices if d.get("id") != device_id]
            changed = len(self._devices) != before
            if changed:
                self._save()
        return changed

    def list(self) -> list[dict]:
        """Public view of live devices (no hashes)."""
        self._reload_if_changed()
        now = time.time()
        return [{
            "id": d.get("id"), "name": d.get("name"),
            "created_at": d.get("created_at"), "last_seen": d.get("last_seen"),
            "roots": d.get("roots"),   # None = all, [] = none
            "manage_devices": bool(d.get("manage_devices")),
            "terminal": bool(d.get("terminal")),
            "passkey": bool(d.get("passkey")),
            "require_passkey": bool(d.get("require_passkey")),
            "locked": bool(d.get("locked")),
            "quiet_exempt": bool(d.get("quiet_exempt")),
            "files": bool(d.get("files")),
            "files_upload": bool(d.get("files_upload")),
        } for d in self._devices if now - d.get("last_seen", 0) < SESSION_IDLE_SECS]
