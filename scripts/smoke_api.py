"""REST-layer smoke test — no Claude subprocess is spawned."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _testdata import isolate  # noqa: E402
isolate("smoke_api")   # never the live ~/.clicker4ai

from fastapi.testclient import TestClient  # noqa: E402

from clicker4ai import main as srv  # noqa: E402
from clicker4ai.auth import issue_pairing_code  # noqa: E402
from clicker4ai.sessions import SessionRunner  # noqa: E402

roots = srv.config.allowed_roots

with TestClient(srv.app) as client:
    r = client.get("/api/state")
    assert r.status_code == 401, f"expected 401, got {r.status_code}"

    r = client.post("/api/login", json={"code": "WRONG-CODE0"})
    assert r.status_code == 401

    code = issue_pairing_code(require_passkey=False)   # passkey_test.py covers the gate
    r = client.post("/api/login", json={"code": code, "name": "smoke test"})
    assert r.status_code == 200, r.text
    r = client.post("/api/login", json={"code": code})
    assert r.status_code == 401, "pairing code must be single use"
    print("pairing login ok")

    r = client.get("/api/state")
    assert r.status_code == 200, r.text
    state = r.json()
    assert "sessions" in state and "defaults" in state
    print(f"state ok — {len(state['sessions'])} stored sessions")

    r = client.get("/api/projects")
    assert r.status_code == 200, r.text
    projs = r.json()["projects"]
    print(f"projects ok — {len(projs)} recent projects; first 3:")
    for p in projs[:3]:
        print(f"   {p['name']:32s} {p['session_count']:3d} sessions  {p['path']}")

    if projs:
        cwd = projs[0]["path"]
        r = client.get("/api/projects/sessions", params={"cwd": cwd})
        assert r.status_code == 200, r.text
        sess = r.json()["sessions"]
        print(f"past sessions ok — {len(sess)} for {cwd}; first:")
        for s in sess[:2]:
            print(f"   {s['session_id'][:8]}  {s['summary'][:70]!r}")
        if sess:
            r = client.get("/api/projects/preview",
                           params={"session_id": sess[0]["session_id"], "cwd": cwd})
            assert r.status_code == 200, r.text
            msgs = r.json()["messages"]
            print(f"preview ok — {len(msgs)} messages in tail")

    r = client.get("/api/library")
    assert r.status_code == 200, r.text
    lib = r.json()
    print(f"library ok — {len(lib['skills'])} skills, {len(lib['commands'])} commands, "
          f"{len(lib['agents'])} agents, {len(lib['mcp_servers'])} mcp servers")
    for s in lib["skills"][:5]:
        print(f"   skill: {s['name']} [{s['scope']}{('/' + s['source']) if s['source'] else ''}]")
    for c in lib["commands"][:5]:
        print(f"   cmd:   /{c['name']} [{c['scope']}]  {c['description'][:40]}")
    for m in lib["mcp_servers"]:
        print(f"   mcp:   {m['name']} ({m['transport']}) [{m['scope']}]")

    # one allowed root → browse starts there; several → list of roots (path None)
    top = roots[0] if len(roots) == 1 else None
    r = client.get("/api/browse")
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["path"] == top, b
    print(f"browse ok — {len(b['dirs'])} dirs under {b['path'] or 'allowed_roots'}")

    r = client.get("/api/browse", params={"path": "/etc"})
    assert r.json()["path"] == top, "browse escaped allowed_roots!"
    print("browse root confinement ok")

    r = client.get("/api/projects/sessions", params={"cwd": "/etc"})
    assert r.status_code == 403, f"projects.sessions escaped root: {r.status_code}"
    r = client.get("/api/projects/preview", params={"session_id": "../../x", "cwd": roots[0]})
    assert r.status_code == 400, f"bad session id accepted: {r.status_code}"
    print("projects jail + session id validation ok")

    # logout revokes this device (keeps devices.json clean)
    r = client.post("/api/logout", json={})
    assert r.status_code == 200, r.text
    r = client.get("/api/state")
    assert r.status_code == 401, "device must be revoked after logout"
    print("logout ok — device revoked")

# ---- rate limit: numbers always, a chat notice only on a refusal ----
# The SDK reports the limit after every request. The app shows the numbers
# in the status line; the chat hears only of a rejection, naming the window
# that ran out (clicker4ai/events.py).
from claude_agent_sdk.types import RateLimitEvent, RateLimitInfo  # noqa: E402

from clicker4ai.events import normalize_message  # noqa: E402


def _rl(status: str, rtype: str | None = None) -> list[dict]:
    raw = {"unifiedWindows": {
        "five_hour": {"utilization": 0.21, "resetsAt": 1790200000},
        "seven_day": {"utilization": 0.99 if status == "rejected" else 0.82,
                      "resetsAt": 1790600000}}}
    info = RateLimitInfo(status=status, rate_limit_type=rtype, raw=raw)
    return normalize_message(RateLimitEvent(rate_limit_info=info, uuid="u", session_id="s"))


for status in ("allowed", "allowed_warning"):
    evs = _rl(status)
    assert [e["kind"] for e in evs] == ["usage"], evs
    assert evs[0]["windows"]["seven_day"]["utilization"] == 0.82, evs
evs = _rl("rejected", "seven_day")
notice = [e for e in evs if e["kind"] == "notice"]
assert len(notice) == 1 and notice[0]["text"] == "Rate limit reached — weekly limit" \
    and notice[0]["resets_at"] == 1790600000, notice
# no type from the CLI: the fullest window is the one that ran out
notice = [e for e in _rl("rejected") if e["kind"] == "notice"]
assert notice[0]["window"] == "seven_day", notice
print("rate limit ok — no warning in the chat, a refusal names its window")

print("\nALL REST SMOKE TESTS PASSED")
