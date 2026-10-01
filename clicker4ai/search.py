"""Search past sessions: their names, your prompts and Claude's replies.

Scanned on demand, no index: every transcript is read as raw bytes and only
those holding each term's literal part are parsed. Tool results, thinking
and subagent (sidechain) rows are not searched.

Query syntax, case-insensitive:
    word        every word must occur in one paragraph (text between blank
                lines of one prompt or reply; the session name is one too)
    "a phrase"  exactly this text
    ?           any one character
    *           any text within one line
    [aą]        one of these characters
A word or phrase matches from the start of a word ("ile" does not find
"file"; "*ile" does). A piece after `*` may also continue the same word
("ser*er" finds "serwer"). Each term needs 3 literal characters in a row:
they are what the raw scan looks for.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .config import CLAUDE_DIR
from .library import _read_claude_json
from .transcript import last_activity_ms, short_path, transcript_stats

LITERAL_MIN = 3
TERMS_MAX = 8
QUERY_MAX = 200
SNIPPET = 160      # characters of context shown around a match
FIND_MAX = 60      # longest matched text handed to the chat's find
# finds tried per line: a huge one-line paste (base64) with a common piece
# would otherwise cost the line's length squared
TRIES_MAX = 200
PARA_SPLIT = re.compile(r"\n[ \t]*\n")


class QueryError(ValueError):
    pass


@dataclass
class Term:
    pieces: list        # str (lowercase) or re.Pattern, split at "*"
    lead_star: bool     # the term starts with "*": its first piece may sit mid-word
    literals: list[bytes]   # raw-scan forms of the longest literal run


def _piece(text: str, phrase: bool):
    """A "*"-free piece: plain lowercase text, or a pattern for ? and [..]."""
    if phrase or ("?" not in text and "[" not in text):
        return text.lower()
    rx, i = "", 0
    while i < len(text):
        ch = text[i]
        if ch == "?":
            rx += "."
        elif ch == "[" and "]" in text[i + 2:]:
            j = text.index("]", i + 2)
            rx += "[" + re.escape(text[i + 1:j]) + "]"
            i = j
        else:
            rx += re.escape(ch)
        i += 1
    return re.compile(rx, re.I)


def _raw_forms(lit: str) -> list[bytes]:
    """The literal as it may appear in a transcript file, compared against
    bytes lowered by bytes.lower() (ASCII only): so an uppercase non-ASCII
    letter ("Łódź") needs its own form. JSON escaping as the CLI writes it."""
    forms = {lit, lit.upper(), lit.capitalize(), lit.title()}
    return sorted({json.dumps(f, ensure_ascii=False)[1:-1].encode().lower() for f in forms})


def parse_query(q: str) -> list[Term]:
    if not isinstance(q, str) or not q.strip():
        raise QueryError("empty query")
    if len(q) > QUERY_MAX:
        raise QueryError(f"query longer than {QUERY_MAX} characters")
    raw = [(p, True) for p in re.findall(r'"([^"]+)"', q)]
    raw += [(w, False) for w in re.sub(r'"[^"]*"?', " ", q).split()]
    raw = [(t, ph) for t, ph in raw if t.strip()]
    if not raw:
        raise QueryError("empty query")
    if len(raw) > TERMS_MAX:
        raise QueryError(f"at most {TERMS_MAX} words")
    terms = []
    for text, phrase in raw:
        if phrase:
            lit, pieces, lead = text, [text.lower()], False
        else:
            runs = re.split(r"[*?]|\[[^\]]*\]", text)
            lit = max(runs, key=len)
            pieces = [_piece(p, False) for p in text.split("*") if p]
            lead = text.startswith("*")
        if len(lit) < LITERAL_MIN or not pieces:
            raise QueryError(f"“{text}”: needs {LITERAL_MIN} letters in a row "
                             "without ? * [ ]")
        terms.append(Term(pieces, lead, _raw_forms(lit.lower())))
    return terms


def _find(line: str, piece, pos: int) -> tuple[int, int]:
    if isinstance(piece, str):
        k = line.find(piece, pos)
        return (k, k + len(piece)) if k >= 0 else (-1, -1)
    m = piece.search(line, pos)
    return (m.start(), m.end()) if m else (-1, -1)


def _word_start(line: str, k: int) -> bool:
    return k == 0 or not line[k - 1].isalnum()


def match_line(line: str, term: Term) -> tuple[int, int] | None:
    """(start, end) of the term in one lowercased line, else None. Pieces
    are found left to right, each at its first fitting place after the one
    before, with no backtracking regex. Only the first piece's start is
    retried: with the word rule below, a later start can succeed where an
    earlier one failed."""
    first, rest = term.pieces[0], term.pieces[1:]
    pos, tries = 0, 0
    while tries < TRIES_MAX:
        k, end = _find(line, first, pos)
        if k < 0:
            return None
        pos = k + 1
        tries += 1
        if not term.lead_star and not _word_start(line, k):
            continue
        at = end
        for piece in rest:
            j, e = _find(line, piece, at)
            if j < 0:
                return None   # not after this start, so after no later one
            # after "*": a new word, or the same word carried on
            while j >= 0 and not _word_start(line, j) and any(c.isspace() for c in line[at:j]):
                tries += 1
                if tries >= TRIES_MAX:
                    return None
                j, e = _find(line, piece, j + 1)
            if j < 0:
                break
            at = e
        else:
            return k, at
    return None


def _texts(path: Path) -> tuple[str | None, str | None, list[tuple[str, str]]]:
    """(custom title, recorded cwd, [(role, text)]) — prompts and replies
    only. Rows made of tool results alone are skipped before parsing: they
    hold most of a transcript's bytes (file contents, images)."""
    title = cwd = None
    parts: list[tuple[str, str]] = []
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"custom-title"' in line:
                try:
                    title = json.loads(line).get("customTitle") or title
                except ValueError:
                    pass
                continue
            user = '"type":"user"' in line
            if not user and '"type":"assistant"' not in line:
                continue
            if user and '"tool_result"' in line and '"type":"text"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            role = d.get("type")
            if role not in ("user", "assistant") or d.get("isSidechain") or d.get("isMeta"):
                continue
            cwd = cwd or d.get("cwd")
            c = (d.get("message") or {}).get("content")
            if isinstance(c, str):
                parts.append((role, c))
            elif isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                        parts.append((role, b["text"]))
    return title, cwd, parts


def _snippet(found: list[tuple[int, str, int, int]]) -> list[list]:
    """Every term's match with some text around it, as [text, is_match]
    pieces; matches far apart (or on other lines) get a window each, the
    windows joined by "…". `found`: (line index, line, start, end)."""
    spans = sorted({(i, s, e): line for i, line, s, e in found}.items())
    # one term: SNIPPET characters from a third before it, as before; more:
    # the room split between them
    ctx = SNIPPET // 3 if len(spans) == 1 else max(24, SNIPPET // (2 * len(spans)))
    windows = []   # [line index, line, a, b, [(start, end)]]
    for (i, s, e), line in spans:
        a = max(0, s - ctx)
        b = min(len(line), max(e, a + SNIPPET) if len(spans) == 1 else e + ctx)
        w = windows[-1] if windows else None
        if w and w[0] == i and a <= w[3]:
            if s >= w[4][-1][1]:   # overlapping matches keep the first
                w[4].append((s, e))
            w[3] = max(w[3], b)
        else:
            windows.append([i, line, a, b, [(s, e)]])
    if len(windows) == 1:   # all in one window: as long as a single match's
        w = windows[0]
        w[3] = min(len(w[1]), max(w[3], w[2] + SNIPPET))
    out: list[list] = []
    for _, line, a, b, marks in windows:
        cut = "…" if a > 0 else ""
        if out:   # the next window: an ellipsis between, once
            gap = " " if cut or out[-1][0].endswith("…") else " … "
            out.append([gap, False])
        at = a
        for s, e in marks:
            pre = line[at:s]
            out.append([cut + (pre.lstrip() if at == a else pre), False])
            out.append([line[s:e], True])
            cut, at = "", e
        out.append([line[at:b].rstrip() + ("…" if b < len(line) else ""), False])
    return [p for p in out if p[0]]


def _folders() -> dict[Path, str]:
    """Transcript folder -> project path, for projects Claude Code knows.
    As in projects.recent_projects the folder decides, since a transcript
    moved there keeps the cwd it was started in."""
    from .projects import _transcript_dir
    out: dict[Path, str] = {}
    for path in _read_claude_json().get("projects") or {}:
        folder = _transcript_dir(path)
        if folder is not None:
            out.setdefault(folder, path)
    return out


def search(query: str, contains, limit: int = 50, offset: int = 0) -> tuple[list[dict], bool]:
    """Sessions matching `query`, newest first: `limit` of them from
    `offset`, and whether more follow. `contains(path)` is the caller's
    scope check. Raises QueryError for a query it cannot run."""
    terms = parse_query(query)
    folders = _folders()
    hits = []
    for path in (CLAUDE_DIR / "projects").glob("*/*.jsonl"):
        try:
            raw = path.read_bytes().lower()
        except OSError:
            continue
        if not all(any(f in raw for f in t.literals) for t in terms):
            continue
        del raw
        title, recorded, parts = _texts(path)
        cwd = folders.get(path.parent) or recorded
        if not cwd or not contains(cwd):
            continue
        paras = [("title", title)] if title else []
        paras += [(role, p) for role, text in parts for p in PARA_SPLIT.split(text)]
        best = None
        # the newest paragraph holding every term: the chat's find starts
        # from the bottom
        for role, para in reversed(paras):
            lines = [(ln, ln.lower()) for ln in para.splitlines()]
            found = []   # (line index, line, start, end), one per term
            for t in terms:
                for i, (ln, low) in enumerate(lines):
                    span = match_line(low, t)
                    if span:
                        found.append((i, ln if len(ln) == len(low) else low, *span))
                        break
                else:
                    break
            else:
                best = (role, found)
                break
        if best:
            hits.append((path, cwd, title, parts, best))
    out = []
    for path, cwd, title, parts, (role, found) in hits:
        # named after the first prompt when it has no name, as the past
        # session lists do (projects.past_sessions)
        first = next((t for r, t in parts if r == "user" and not t.startswith("<")), "")
        # every term's text, once each, for the chat's find to mark
        find: list[str] = []
        for _, line, start, end in found:
            m = line[start:end]
            if len(m) <= FIND_MAX and m.lower() not in (f.lower() for f in find):
                find.append(m)
        out.append({
            "session_id": path.stem,
            "summary": title or first[:64].rstrip() or "(untitled)",
            "cwd": cwd,
            "cwd_short": short_path(cwd),
            "last_modified_ms": last_activity_ms(path),
            "role": role,
            "snippet": _snippet(found),
            "find": find,
            "_path": path,
        })
    out.sort(key=lambda x: x["last_modified_ms"] or 0, reverse=True)
    page = out[offset:offset + limit]
    for s in page:   # only the page shown: a stats read per card
        stats = transcript_stats(s.pop("_path"))
        s["context_tokens"], s["model"] = stats["context_tokens"], stats["model"]
    return page, len(out) > offset + limit
