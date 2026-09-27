"""Project discovery, past-session listing and directory browsing.

Past sessions come from the SDK's transcript readers (same data the CLI's
/resume picker uses). Recent projects merge ~/.claude.json project entries
with ~/.claude/projects transcript directories.
"""

from __future__ import annotations

import time
from pathlib import Path

from claude_agent_sdk import get_session_messages, list_sessions

from .config import INCOGNITO_DIR, PROTECTED_DIRS
from .library import _read_claude_json
from .transcript import last_activity_ms, short_path, transcript_stats
from .watch import project_dir, transcript_path


_recent_cache: tuple[float, list[dict]] | None = None

try:
    # Private SDK helper (may move between SDK versions): the CLI's
    # directory munging, including its long-path hashing.
    from claude_agent_sdk._internal.sessions import _find_project_dir
except ImportError:  # pragma: no cover — SDK internals moved
    _find_project_dir = None


def _transcript_dir(project: str) -> Path | None:
    if _find_project_dir is not None:
        try:
            return _find_project_dir(project)
        except Exception:
            pass
    return project_dir(project)


def _session_dir(session_id: str, cwd: str, recorded: str | None) -> tuple[str, Path | None]:
    """(directory, transcript) of a past session listed under project `cwd`.
    A transcript moved into this project's folder keeps the cwd it was
    started in (every row records it, and the SDK reports the first), so
    the folder it lies in decides; only worktree sessions, which have a
    folder of their own, keep their recorded cwd."""
    path = transcript_path(session_id, cwd)
    if path is not None and path.parent == _transcript_dir(cwd):
        return cwd, path
    return recorded or cwd, path or transcript_path(session_id, recorded)


def recent_projects(limit: int = 40) -> list[dict]:
    """Known project paths ordered by most recent session activity."""
    global _recent_cache
    if _recent_cache and time.monotonic() - _recent_cache[0] < 30:
        return _recent_cache[1][:limit]

    known = list(_read_claude_json().get("projects") or {})
    try:
        infos = list_sessions()
    except Exception:
        infos = []

    # A transcript moved into another project's folder (the directory was
    # renamed) keeps the cwd it was started in, so the folder it lies in
    # decides, as in _session_dir; otherwise a renamed project lists its
    # sessions under the old, missing path.
    home_of: dict[str, str] = {}
    files: dict[str, Path] = {}
    folders: dict[Path, str] = {}
    for path in known + [i.cwd for i in infos if i.cwd]:
        folder = _transcript_dir(path)
        if folder is not None and folder not in folders:
            folders[folder] = path
    for folder, path in folders.items():
        for f in folder.glob("*.jsonl"):
            home_of[f.stem] = path
            files[f.stem] = f

    # cwd -> last activity (ms)
    activity: dict[str, float] = {}
    counts: dict[str, int] = {}
    try:
        for info in infos:
            cwd = home_of.get(info.session_id) or info.cwd
            if not cwd:
                continue
            counts[cwd] = counts.get(cwd, 0) + 1
            # the last message, not the file's mtime (see last_activity_ms)
            ts = last_activity_ms(files.get(info.session_id)) or info.last_modified or 0
            if ts > activity.get(cwd, 0):
                activity[cwd] = ts
    except Exception:
        pass

    # Include projects known to claude.json even without readable transcripts
    for path in known:
        activity.setdefault(path, 0)
        counts.setdefault(path, 0)

    items = []
    for path, ts in activity.items():
        p = Path(path)
        if p.is_relative_to(INCOGNITO_DIR):
            continue   # incognito chats are not projects (sessions.py)
        items.append({
            "path": path,
            "name": p.name or path,
            "parent": str(p.parent),
            "exists": p.is_dir(),
            "last_active_ms": int(ts),
            "session_count": counts.get(path, 0),
        })
    items.sort(key=lambda x: x["last_active_ms"], reverse=True)
    _recent_cache = (time.monotonic(), items)
    return items[:limit]


def past_sessions(cwd: str, limit: int = 25) -> list[dict]:
    """Resumable transcript sessions for a project directory."""
    out = []
    try:
        infos = list_sessions(directory=cwd, limit=limit)
    except Exception:
        return out
    for info in infos:
        where, path = _session_dir(info.session_id, cwd, info.cwd)
        stats = transcript_stats(path)
        # An unnamed session is called after its first prompt, cut like the
        # runner's automatic title (sessions.py), not after the SDK's
        # summary — that is the last prompt, so it changed with every turn.
        auto = (info.first_prompt or "")[:64].rstrip()
        out.append({
            "session_id": info.session_id,
            "summary": info.custom_title or auto or info.summary or "(untitled)",
            "first_prompt": (info.first_prompt or "")[:200],
            "last_modified_ms": last_activity_ms(path) or info.last_modified,
            "created_at_ms": info.created_at,
            "git_branch": info.git_branch,
            "cwd": where,
            "cwd_short": short_path(where),
            "context_tokens": stats["context_tokens"],
            "model": stats["model"],
        })
    out.sort(key=lambda x: x["last_modified_ms"] or 0, reverse=True)
    return out


def session_preview(session_id: str, cwd: str | None, tail: int = 12) -> list[dict]:
    """Last few messages of a past session, simplified for a peek UI."""
    try:
        messages = get_session_messages(session_id, directory=cwd)
    except Exception:
        return []
    simplified: list[dict] = []
    for m in messages:
        raw = getattr(m, "message", None) or {}
        role = getattr(m, "type", None) or raw.get("role")
        content = raw.get("content") if isinstance(raw, dict) else None
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        parts.append(f"[tool: {block.get('name')}]")
                else:
                    t = getattr(block, "text", None)
                    if t:
                        parts.append(t)
            text = "\n".join(p for p in parts if p)
        if role in ("user", "assistant") and text.strip():
            simplified.append({"role": role, "text": text.strip()[:600]})
    return simplified[-tail:]


def session_cwd(session_id: str, cwd: str) -> str | None:
    """Directory of a past session (see _session_dir) if that session belongs
    to project directory `cwd` (incl. its worktrees), else None. Used to
    keep previews and resumes inside a device's scope: the SDK readers
    would otherwise find a session id in any project."""
    try:
        infos = list_sessions(directory=cwd)
    except Exception:
        return None
    for info in infos:
        if info.session_id == session_id:
            return _session_dir(session_id, cwd, info.cwd)[0]
    return None


def _is_project(p: Path) -> bool:
    try:
        return (p / ".git").exists() or (p / ".claude").exists() \
            or (p / "pyproject.toml").exists() or (p / "package.json").exists()
    except OSError:
        return False


def browse(path: str | None, roots: list[Path]) -> dict:
    """List sub-directories for the new-session folder picker, confined to
    `roots` (resolved). With several roots and no path, list the roots
    themselves (path None); "parent": "" means "back to that list"."""
    p = Path(path).expanduser().resolve() if path else None
    if p is not None and any(p.is_relative_to(d) for d in PROTECTED_DIRS):
        p = None   # server data, incognito chats, ~/.claude — even under a root
    hits = [r for r in roots if p is not None and p.is_relative_to(r)]
    root = max(hits, key=lambda r: len(r.parts)) if hits else None
    if root is None or not p.is_dir():
        if len(roots) != 1:
            return {"path": None, "parent": None,
                    "dirs": [{"name": str(r), "path": str(r), "is_project": _is_project(r)}
                             for r in roots]}
        root = p = roots[0]
    dirs = []
    try:
        for child in sorted(p.iterdir(), key=lambda c: c.name.lower()):
            if not child.is_dir() or child.name.startswith(".") \
                    or child in PROTECTED_DIRS:
                continue
            dirs.append({"name": child.name, "path": str(child),
                         "is_project": _is_project(child)})
    except OSError:
        pass
    if p != root:
        parent = str(p.parent)
    else:
        parent = "" if len(roots) > 1 else None
    return {"path": str(p), "parent": parent, "dirs": dirs}


NAME_BYTES_MAX = 255


def make_dir(parent: Path, name: str) -> Path:
    """Create `name` inside `parent` (already jailed by the caller) for the
    folder picker. One level only, never an existing folder, never a hidden
    one (browse would not list it) and never under the incognito tree."""
    name = name.strip()
    if not name or name in (".", "..") or name.startswith(".") \
            or "/" in name or "\0" in name \
            or len(name.encode()) > NAME_BYTES_MAX:
        raise ValueError("invalid folder name")
    if parent.is_relative_to(INCOGNITO_DIR):
        raise PermissionError("not here")
    if not parent.is_dir():
        raise FileNotFoundError("parent folder is gone")
    child = parent / name
    child.mkdir()   # FileExistsError if taken
    return child
