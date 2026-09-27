"""Project files: list, view, download and (optionally) upload, without
Claude — plain file access for a device that has the grant.

Grants come from the CLI only (`--files / --files-upload` on `pair` or
`devices set`):
"files" lets a device list, view and download, "files_upload" also lets it
upload and implies "files". The reach is the device's scope (scope.py): its
roots, minus PROTECTED_DIRS. Hidden files are listed; SKIP_NAMES are not,
and no path through one of them is served.

Viewing never runs anything from a project: text goes to the app as JSON
(files.text, shown as plain text), images are served with
`Content-Security-Policy: sandbox` and `nosniff`, so an SVG or an HTML file
opened straight from its URL cannot run a script on this origin. Every
other file is served only as an attachment.

Uploads never overwrite: an existing name is refused (409) and the app asks
for another one.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

from .config import INCOGNITO_DIR
from .scope import Scope

SKIP_NAMES = frozenset({".git", "node_modules", ".venv"})
TEXT_MAX = 1024 * 1024            # files.text shows at most this much
UPLOAD_MAX = 50 * 1024 * 1024
NAME_BYTES_MAX = 255
IMAGE_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".avif": "image/avif",
    ".svg": "image/svg+xml", ".ico": "image/x-icon", ".bmp": "image/bmp",
}


class FilesError(Exception):
    """code: forbidden | not_found | bad_request | exists | too_large"""
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def can_view(device: dict | None) -> bool:
    return bool(device and (device.get("files") or device.get("files_upload")))


def can_upload(device: dict | None) -> bool:
    return bool(device and device.get("files_upload"))


def image_type(p: Path) -> str | None:
    return IMAGE_TYPES.get(p.suffix.lower())


def _skipped(p: Path, scope: Scope) -> bool:
    """Does the path go through a SKIP_NAMES directory below its root?"""
    root = scope.root_of(p)
    rel = p.relative_to(root).parts if root else p.parts
    return any(part in SKIP_NAMES for part in rel)


def jail(raw: str | None, scope: Scope) -> Path:
    """Resolved path inside the scope and outside SKIP_NAMES, else FilesError."""
    p = scope.resolve(raw) if isinstance(raw, str) else None
    if p is None or _skipped(p, scope):
        raise FilesError("forbidden", "outside allowed folders")
    return p


def list_dir(raw: str | None, scope: Scope) -> dict:
    """A directory's entries: folders first, then files, each by name.
    No path (or several roots and a path outside them) lists the roots.
    "parent": a path, "" = back to the roots, None = nothing above."""
    if not raw:
        if len(scope.roots) == 1:
            raw = str(scope.roots[0])
        else:
            return {"path": None, "parent": None,
                    "entries": [{"name": str(r), "path": str(r), "dir": True}
                                for r in scope.roots]}
    p = jail(raw, scope)
    if not p.is_dir():
        raise FilesError("not_found", "no such folder")
    entries = []
    try:
        children = list(p.iterdir())
    except OSError as e:
        raise FilesError("forbidden", f"cannot read this folder: {e.strerror}")
    for child in children:
        if child.name in SKIP_NAMES:
            continue
        # a symlink out of the scope, or into a protected directory, is left out
        real = scope.resolve(child)
        if real is None:
            continue
        try:
            st = real.stat()
        except OSError:
            continue
        is_dir = stat.S_ISDIR(st.st_mode)
        if not is_dir and not stat.S_ISREG(st.st_mode):
            continue   # sockets, fifos, devices
        entries.append({"name": child.name, "path": str(child), "dir": is_dir,
                        "size": None if is_dir else st.st_size,
                        "mtime": int(st.st_mtime * 1000),
                        "image": not is_dir and image_type(child) is not None})
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    root = scope.root_of(p)
    if root is not None and p != root:
        parent = str(p.parent)
    else:
        parent = "" if len(scope.roots) > 1 else None
    return {"path": str(p), "parent": parent, "entries": entries}


def read_text(raw: str, scope: Scope) -> dict:
    """At most TEXT_MAX bytes of a file as text; "binary" true (and no
    text) when it does not look like UTF-8 text."""
    p = jail(raw, scope)
    if not p.is_file():
        raise FilesError("not_found", "no such file")
    size = p.stat().st_size
    with open(p, "rb") as f:
        data = f.read(TEXT_MAX)
    if b"\0" in data[:8192]:
        return {"size": size, "binary": True}
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        # a multi-byte character cut at TEXT_MAX is still text
        if size > TEXT_MAX and e.start >= len(data) - 3:
            text = data[:e.start].decode("utf-8")
        else:
            return {"size": size, "binary": True}
    return {"size": size, "binary": False, "text": text, "truncated": size > TEXT_MAX}


def check_name(name: str) -> str:
    name = name.strip() if isinstance(name, str) else ""
    if not name or name in (".", "..") or "/" in name or "\0" in name \
            or name in SKIP_NAMES or len(name.encode()) > NAME_BYTES_MAX:
        raise FilesError("bad_request", "invalid file name")
    return name


def upload_target(dir_raw: str, name: str, scope: Scope) -> Path:
    """Where an upload goes; refuses before any byte is read when the name
    is taken or the folder is not a place for it."""
    d = jail(dir_raw, scope)
    if d.is_relative_to(INCOGNITO_DIR):
        raise FilesError("forbidden", "cannot upload here")
    if not d.is_dir():
        raise FilesError("not_found", "no such folder")
    target = d / check_name(name)
    if os.path.lexists(target):
        raise FilesError("exists", "a file with that name already exists")
    return target


class Upload:
    """Bytes go to a hidden temporary file next to the target; `finish`
    links it under the real name, which fails if that name appeared
    meanwhile, so nothing is ever overwritten."""

    def __init__(self, target: Path):
        self.target = target
        fd, tmp = tempfile.mkstemp(prefix=".c4ai-upload-", dir=target.parent)
        self.tmp = Path(tmp)
        self.f = os.fdopen(fd, "wb")
        self.size = 0

    def write(self, chunk: bytes) -> None:
        self.size += len(chunk)
        if self.size > UPLOAD_MAX:
            raise FilesError("too_large", f"larger than {UPLOAD_MAX // (1024 * 1024)} MB")
        self.f.write(chunk)

    def finish(self) -> None:
        self.f.close()
        os.chmod(self.tmp, 0o644)
        try:
            os.link(self.tmp, self.target)
        except FileExistsError:
            raise FilesError("exists", "a file with that name already exists")
        finally:
            self.tmp.unlink(missing_ok=True)

    def abort(self) -> None:
        if not self.f.closed:
            self.f.close()
        self.tmp.unlink(missing_ok=True)
