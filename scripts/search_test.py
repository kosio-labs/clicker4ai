"""Search over past sessions (clicker4ai/search.py), in process.

Builds a throwaway CLAUDE_CONFIG_DIR with hand-made transcripts under
.scratch/ (removed at exit), so the live ~/.claude is never read. No
`claude` process, no server.

    timeout -s KILL 300 .venv/bin/python scripts/search_test.py
"""

import atexit
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _testdata import ROOT, isolate  # noqa: E402

# environment first: clicker4ai reads it at import (config.CLAUDE_DIR)
isolate("search")
base = Path(tempfile.mkdtemp(prefix="c4ai-search-claude-", dir=ROOT / ".scratch"))
atexit.register(shutil.rmtree, base, True)
os.environ["CLAUDE_CONFIG_DIR"] = str(base / "claude")

from clicker4ai import search  # noqa: E402
from clicker4ai.scope import Scope  # noqa: E402

inside = (base / "work" / "proj").resolve()
outside = (base / "elsewhere" / "proj").resolve()
for d in (inside, outside):
    d.mkdir(parents=True)
scope = Scope([(base / "work").resolve()])

n = 0


def transcript(cwd: Path, rows: list[dict], title: str | None = None) -> str:
    global n
    n += 1
    sid = f"00000000-0000-4000-8000-{n:012d}"
    folder = base / "claude" / "projects" / ("-" + str(cwd).strip("/").replace("/", "-"))
    folder.mkdir(parents=True, exist_ok=True)
    lines = []
    for i, r in enumerate(rows):
        r = {"cwd": str(cwd), "sessionId": sid,
             "timestamp": f"2026-09-{10 + n:02d}T10:{i:02d}:00.000Z", **r}
        lines.append(json.dumps(r, ensure_ascii=False, separators=(",", ":")))
    if title:
        lines.append(json.dumps({"type": "custom-title", "customTitle": title,
                                 "sessionId": sid}, ensure_ascii=False,
                                separators=(",", ":")))
    (folder / f"{sid}.jsonl").write_text("\n".join(lines) + "\n")
    return sid


def user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def claude(text):
    return {"type": "assistant", "message": {"role": "assistant",
            "content": [{"type": "text", "text": text}]}}


def tool_result(text):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "x", "content": text}]}}


A = transcript(inside, [
    user("ok, a small LXC for gitea with silverbullet"),
    claude("For that, ile pamięci ram: 1 GB is enough.\nThe file lists a program."),
    user('he said "hello" twice'),
])
B = transcript(inside, [
    user("the file is part of the program"),
    claude("Rdzeń jednostki ma 4 wątki. ŁÓDŹ nocą."),
])
C = transcript(inside, [user("nothing here")], title="Serwer Gitea")
D = transcript(inside, [user("look"), tool_result("secretword in a file")])
E = transcript(inside, [user("main"), {**claude("hiddenword from a subagent"), "isSidechain": True}])
F = transcript(outside, [user("gitea ram outside the scope")])
G = transcript(inside, [user("x " + "a" * 200_000)])
H = transcript(inside, [claude("Plan for forgejo:\n- 2 cores, 2 GB memory\n\n"
                               "zzzebra comes later")])


def ids(q):
    found, _ = search.search(q, scope.contains)
    return {s["session_id"] for s in found}


fails = 0


def check(what, cond, got=None):
    global fails
    print(("ok   " if cond else "FAIL ") + what + ("" if cond or got is None else f": {got!r}"))
    fails += not cond


check("all words in one line", ids("gitea silverbullet") == {A})
check("all words in one paragraph, across lines", ids("forgejo memory") == {H})
check("not across paragraphs", ids("forgejo zzzebra") == set())
check("not across messages", ids("gitea ram") == set())
check("scope: a session outside the roots is left out", F not in ids("gitea ram"))
check("* crosses words in one line", ids("ile*ram") == {A})
check("words match from their start", ids("ile") == {A})
check("leading * finds mid-word", ids("*ile") == {A, B})
check("? is one character", ids("rdze?") == {B})
check("[..] is one of", ids("rdze[nń]") == {B})
check("no diacritics folding", ids("rdzen") == set())
check("uppercase Polish letters", ids("łódź") == {B})
check("* carries on within a word", ids("ser*er") == {C})
check("session name is searched", ids("serwer") == {C})
check("phrase", ids('"pamięci ram"') == {A} and ids('"ram pamięci"') == set())
check("quotes in the text (JSON escaped)", ids('"hello" twice') == {A})
check("tool results are not searched", ids("secretword") == set())
check("subagent rows are not searched", ids("hiddenword") == set())

found, _ = search.search("ile*ram", scope.contains)
s = found[0]
check("snippet and find text", [p[0] for p in s["snippet"] if p[1]] == ["ile pamięci ram"]
      and s["find"] == ["ile pamięci ram"] and s["role"] == "assistant")
check("unnamed session is called after its first prompt",
      s["summary"].startswith("ok, a small LXC"))


def one(q):
    found, _ = search.search(q, scope.contains)
    s = found[0]
    return "".join(p[0] for p in s["snippet"]), [p[0] for p in s["snippet"] if p[1]], s["find"]


text, hits, find = one("gitea silverbullet")
check("every word marked in the snippet", hits == ["gitea", "silverbullet"]
      and find == ["gitea", "silverbullet"]
      and text == "ok, a small LXC for gitea with silverbullet")
text, hits, find = one("memory forgejo")
check("words on two lines: a window each", hits == ["forgejo", "memory"]
      and find == ["memory", "forgejo"] and " … " in text, text)
text, hits, find = one("gitea gitea")
check("a word given twice is marked and found once",
      [h.lower() for h in hits] == ["gitea"] and len(find) == 1, (hits, find))
I = transcript(inside, [user("qqstart " + "filler " * 60 + "qqend")])
text, hits, _ = one("qqstart qqend")
check("far apart in one line: an ellipsis between", hits == ["qqstart", "qqend"]
      and "…" in text and len(text) < 200, text)

for bad in ("r*m", "ab", "", "a?c"):
    try:
        search.search(bad, scope.contains)
        check(f"refused: {bad!r}", False)
    except search.QueryError:
        check(f"refused: {bad!r}", True)

t0 = time.perf_counter()
ids("aaa*b")
dt = time.perf_counter() - t0
check(f"a 200k-character line stays fast ({dt:.2f}s)", dt < 2)

print("FAILED" if fails else "all passed")
sys.exit(1 if fails else 0)
