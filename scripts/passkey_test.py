"""Passkey end-to-end test with a software authenticator — no browser.

Runs the whole WebAuthn flow the phone would run (register, login,
step-up) against the real server routes, with a throwaway ES256 key
standing in for the Secure Enclave. Uses its own C4AI_DATA_DIR under
.scratch/, so the live ~/.clicker4ai is never touched.

    .venv/bin/python scripts/passkey_test.py
"""

import hashlib
import json
import os
import subprocess
import time
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

(ROOT / ".scratch").mkdir(exist_ok=True)
DATA_DIR = Path(tempfile.mkdtemp(prefix="c4ai-passkey-", dir=ROOT / ".scratch"))
os.environ["C4AI_DATA_DIR"] = str(DATA_DIR)
(DATA_DIR / "config.json").write_text(json.dumps(
    {"allowed_roots": [str(Path.home() / "work")], "public_url": "https://rc.example.test"}))

import cbor2  # noqa: E402
from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url  # noqa: E402

from clicker4ai import main as srv  # noqa: E402
from clicker4ai import passkey  # noqa: E402
from clicker4ai.auth import issue_pairing_code  # noqa: E402

ORIGIN = "https://rc.example.test"
RP_ID = "rc.example.test"
AAGUID = b"\x00" * 16


class SoftAuthenticator:
    """The smallest authenticator that satisfies verification: one ES256
    credential, user presence + user verification always set, sign count
    fixed at 0 (exactly what Apple's passkeys report)."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(32)

    def _cose_key(self) -> bytes:
        nums = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1,
                            -2: nums.x.to_bytes(32, "big"),
                            -3: nums.y.to_bytes(32, "big")})

    def _client_data(self, kind: str, challenge: str) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge,
                           "origin": ORIGIN, "crossOrigin": False}).encode()

    def _auth_data(self, flags: int, attested: bool) -> bytes:
        data = hashlib.sha256(RP_ID.encode()).digest() + bytes([flags]) + (0).to_bytes(4, "big")
        if attested:
            cose = self._cose_key()
            data += AAGUID + len(self.cred_id).to_bytes(2, "big") + self.cred_id + cose
        return data

    def create(self, options: dict) -> dict:
        client_data = self._client_data("webauthn.create", options["challenge"])
        # UP | UV | BE | BS | AT — a synced, user-verified passkey
        auth_data = self._auth_data(0x01 | 0x04 | 0x08 | 0x10 | 0x40, True)
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {
            "id": bytes_to_base64url(self.cred_id),
            "rawId": bytes_to_base64url(self.cred_id),
            "type": "public-key",
            "clientExtensionResults": {},
            "response": {"clientDataJSON": bytes_to_base64url(client_data),
                         "attestationObject": bytes_to_base64url(att),
                         "transports": ["internal", "hybrid"]},
        }

    def get(self, options: dict) -> dict:
        client_data = self._client_data("webauthn.get", options["challenge"])
        auth_data = self._auth_data(0x01 | 0x04 | 0x08 | 0x10, False)
        signature = self.key.sign(
            auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        return {
            "id": bytes_to_base64url(self.cred_id),
            "rawId": bytes_to_base64url(self.cred_id),
            "type": "public-key",
            "clientExtensionResults": {},
            "response": {"clientDataJSON": bytes_to_base64url(client_data),
                         "authenticatorData": bytes_to_base64url(auth_data),
                         "signature": bytes_to_base64url(signature),
                         "userHandle": None},
        }


def main() -> None:
    # the server would do this from the CLI; TestClient speaks http, so the
    # cookie stays non-Secure while the passkey RP is the https public URL
    passkey.configure(ORIGIN, 8780)
    assert passkey.available(), "passkeys should be available for an https URL"
    assert passkey.rp_id() == RP_ID

    auth = SoftAuthenticator()
    with TestClient(srv.app) as c:
        r = c.get("/api/passkey")
        assert r.json() == {"available": True, "has_credentials": False,
                            "device_passkey": False, "device": None, "rp_id": RP_ID,
                            "elevation_secs": passkey.ELEVATION_SECS}, r.text
        assert c.post("/api/passkey/login/begin", json={}).status_code == 400, \
            "login must refuse while no passkey is registered"
        print("status + empty login ok")

        # pair a device the way a phone would, with a True View grant
        code = issue_pairing_code(terminal=True, manage_devices=True)
        assert c.post("/api/login", json={"code": code,
                                 "name": "passkey test"}).status_code == 200

        # --- a pairing code requires a passkey by default ---
        assert c.get("/api/state").status_code == 428, "no key yet: only registering"
        assert c.get("/api/projects").status_code == 428
        with c.websocket_connect("/ws", headers={"origin": "http://testserver"}) as ws:
            try:
                ws.receive_text()
                raise AssertionError("the socket must close for a device owing a key")
            except Exception as e:
                assert getattr(e, "code", None) == 4428, e
        st = c.get("/api/passkey").json()
        assert st["device"]["name"] == "passkey test" and st["device"]["require_passkey"]
        print("passkey requirement ok — REST 428, socket 4428, registration still open")

        # --- register ---
        opts = c.post("/api/passkey/register/begin", json={}).json()
        assert opts["rp"]["id"] == RP_ID and opts["authenticatorSelection"][
            "residentKey"] == "required", opts
        r = c.post("/api/passkey/register/finish",
                   json={"credential": auth.create(opts), "name": "Test iPhone"})
        assert r.status_code == 200, r.text
        assert c.get("/api/state").status_code == 200, "a key lifts the gate"
        assert c.get("/api/state").json()["passkey_device"] is True
        keys = c.get("/api/passkeys").json()["passkeys"]
        assert len(keys) == 1 and keys[0]["name"] == "Test iPhone" and keys[0]["backed_up"]
        print("registration ok — credential stored, carrying no grants")

        # a replayed registration response must not be accepted twice
        assert c.post("/api/passkey/register/finish",
                      json={"credential": auth.create(opts)}).status_code == 400, \
            "challenge must be single use"
        print("challenge replay refused")

        # --- step-up ---
        opts = c.post("/api/passkey/stepup/begin", json={}).json()
        assert not opts.get("allowCredentials"), "step-up should not list credentials"
        bad = auth.get(opts)
        bad["response"]["signature"] = bytes_to_base64url(b"\x30\x00" * 8)
        assert c.post("/api/passkey/stepup/finish",
                      json={"credential": bad}).status_code == 400, \
            "a bad signature must be rejected"
        opts = c.post("/api/passkey/stepup/begin", json={}).json()
        r = c.post("/api/passkey/stepup/finish", json={"credential": auth.get(opts)})
        assert r.status_code == 200, r.text
        print("step-up ok — bad signature rejected, good one elevates")

        # --- the credential must carry no grants at all ---
        cred = passkey.list_public()[0]
        assert "terminal" not in cred and "manage_devices" not in cred \
            and "roots" not in cred, cred
        device_id = c.get("/api/passkeys").json()["passkeys"][0]["used_by"][0]

        # --- what the browser needs to prune the keychain ---
        sig = c.get("/api/passkeys/signal").json()
        assert sig["rp_id"] == RP_ID and sig["user_id"], sig
        assert sig["credential_ids"] == [bytes_to_base64url(auth.cred_id)], sig
        print("signal payload ok — rp id, owner handle and accepted ids")

        # --- login on a "second" client (fresh cookie jar) ---
        c.cookies.clear()
        assert c.get("/api/state").status_code == 401
        opts = c.post("/api/passkey/login/begin", json={}).json()
        r = c.post("/api/passkey/login/finish",
                   json={"credential": auth.get(opts), "name": "second phone"})
        assert r.status_code == 200, r.text
        state = c.get("/api/state").json()
        # the registering device had True View and device management; the
        # session born from its passkey must have neither, nor any folder
        assert not state["can_terminal"], "passkey login must not carry True View"
        assert not state["has_roots"], "passkey login must start with no folders"
        assert state["passkey_device"], state
        assert state["sessions"] == [] and not c.get("/api/projects").json()["projects"]
        assert c.get("/api/browse").json()["dirs"] == [], "empty scope must show nothing"
        used = c.get("/api/passkeys").json()["passkeys"][0]["used_by"]
        assert len(used) == 2 and device_id in used, used
        new_id = [d for d in used if d != device_id][0]
        print("passkey login ok — empty session, no folders, no True View")

        # --- rename: the label only, from a device that used the key ---
        kid = keys[0]["id"]
        r = c.patch(f"/api/passkeys/{kid}", json={"name": "  Shared   key "})
        assert r.status_code == 200 and r.json()["name"] == "Shared key", r.text
        assert c.get("/api/passkeys").json()["passkeys"][0]["name"] == "Shared key"
        assert c.patch(f"/api/passkeys/{kid}", json={"name": "  "}).status_code == 400
        assert c.patch(f"/api/passkeys/{kid}", json={"name": "x" * 65}).status_code == 400
        assert c.patch("/api/passkeys/nope", json={"name": "x"}).status_code == 404
        # the CLI takes an id prefix, like `passkeys remove`
        out = subprocess.run([sys.executable, "-m", "clicker4ai", "passkeys", "rename",
                              kid[:8], "Test", "iPhone"],
                             cwd=Path(__file__).resolve().parent.parent,
                             env={**os.environ, "C4AI_DATA_DIR": str(DATA_DIR)},
                             capture_output=True, text=True)
        assert out.returncode == 0 and "Test iPhone" in out.stdout, out
        assert passkey.list_public()[0]["name"] == "Test iPhone"
        # an id (or a name) starting with "-" is data, not an option: about
        # one real id in 64 does
        def cli(*a):
            return subprocess.run([sys.executable, "-m", "clicker4ai", "passkeys", *a],
                                  cwd=Path(__file__).resolve().parent.parent,
                                  env={**os.environ, "C4AI_DATA_DIR": str(DATA_DIR)},
                                  capture_output=True, text=True)
        for a in (("remove", "-nosuch"), ("rename", "-nosuch", "x")):
            out = cli(*a)
            assert out.returncode == 1 and "No such passkey: -nosuch" in out.stderr, out
        out = cli("rename", kid[:8], "-Test", "iPhone")
        assert out.returncode == 0 and passkey.list_public()[0]["name"] == "-Test iPhone", out
        assert cli("rename", kid[:8], "Test", "iPhone").returncode == 0
        assert cli("rename", "-h").returncode == 0   # help still works
        print("rename ok — app and CLI, empty / too long / unknown refused, "
              "leading '-' taken as data")

        # names are unique, case-insensitive: rename refuses a taken one,
        # registration numbers it (the store holds one key here, so check
        # the helpers against a made-up second one)
        other = [{"id": "other", "name": "Work Mac"}, {"id": kid, "name": "Test iPhone"}]
        assert passkey._name_taken(other, "work mac") and \
            not passkey._name_taken(other, "Test iPhone", except_id=kid)
        assert passkey._unique_name(other, "WORK MAC") == "WORK MAC (2)"
        other.append({"id": "third", "name": "WORK MAC (2)"})
        assert passkey._unique_name(other, "Work Mac") == "Work Mac (3)"
        long = "y" * passkey.NAME_MAX
        assert passkey._unique_name([{"id": "z", "name": long}], long) == "y" * 60 + " (2)"
        print("unique names ok — taken refused, registration numbered, fits the limit")

        # the CLI gives it folders; the credential was not involved
        assert srv.devices.set_roots(new_id, [str(Path.home() / "work")])
        assert c.get("/api/state").json()["has_roots"], "set-roots must take effect"
        print("cli set-roots ok — grants come from the CLI, not the key")

        # --- lock / unlock: the cookie alone is no longer enough ---
        assert c.post("/api/lock", json={}).status_code == 200
        assert c.get("/api/state").status_code == 423, "a locked session must refuse"
        assert c.get("/api/projects").status_code == 423
        opts = c.post("/api/unlock/begin", json={}).json()
        bad = auth.get(opts)
        bad["response"]["signature"] = bytes_to_base64url(b"\x30\x00" * 8)
        assert c.post("/api/unlock/finish", json={"credential": bad}).status_code == 400
        assert c.get("/api/state").status_code == 423, "a bad unlock must not wake it"
        opts = c.post("/api/unlock/begin", json={}).json()
        assert c.post("/api/unlock/finish",
                      json={"credential": auth.get(opts)}).status_code == 200
        assert c.get("/api/state").status_code == 200, "unlock must restore access"
        assert c.get("/api/state").json()["has_roots"], "grants survive a lock"
        print("lock/unlock ok — cookie kept, grants kept, passkey required")

        # A locked session must not slide its idle expiry, or an app left
        # on the lock screen would keep the device alive for ever. Age the
        # record past the write threshold, then poke it while locked.
        import clicker4ai.auth as auth_mod
        stale = time.time() - auth_mod.LAST_SEEN_WRITE_SECS - 60
        srv.devices._update(new_id, lambda d: d.__setitem__("last_seen", stale))
        srv.devices.set_locked(new_id, True)
        assert c.get("/api/state").status_code == 423
        assert next(d for d in srv.devices.list()
                    if d["id"] == new_id)["last_seen"] == stale, \
            "a locked device must not refresh last_seen"
        # unlocking, and only then, starts the clock again
        opts = c.post("/api/unlock/begin", json={}).json()
        assert c.post("/api/unlock/finish",
                      json={"credential": auth.get(opts)}).status_code == 200
        assert c.get("/api/state").status_code == 200
        assert next(d for d in srv.devices.list()
                    if d["id"] == new_id)["last_seen"] > stale, "unlock must resume it"
        print("locked sessions keep ageing ok")

        # --- idle lock: a device with a key sleeps after lock_idle_minutes ---
        srv.devices.idle_lock_secs = 900
        assert c.get("/api/state").status_code == 200, "fresh activity: still awake"
        srv.devices._active[new_id] = time.time() - 901
        srv.devices._update(new_id, lambda d: d.__setitem__("last_seen", time.time() - 901))
        assert c.get("/api/state").status_code == 423, "idle past the limit must lock"
        # the CLI unlock cannot reach _active; last_seen carries it
        srv.devices.set_locked(new_id, False)
        assert c.get("/api/state").status_code == 200, "an unlock must not relock at once"
        srv.devices.idle_lock_secs = 0
        print("idle lock ok — locks when idle, a CLI unlock sticks")

        # --- revoking a device only unlists the credential ---
        # same two calls as rpc._devices_revoke and the CLI `revoke`
        srv.devices.revoke(new_id)
        passkey.drop_device(new_id)
        keys = passkey.list_public()
        assert len(keys) == 1 and keys[0]["used_by"] == [device_id], keys
        print("revoke unlists the device but keeps the credential")

        # removing the credential (the app route) clears the key mark on every
        # device that used it; one that must carry a key is gated again
        opts = c.post("/api/passkey/login/begin", json={}).json()
        assert c.post("/api/passkey/login/finish",
                      json={"credential": auth.get(opts), "name": "third"}).status_code == 200
        third = c.get("/api/passkey").json()["device"]["id"]
        r = c.request("DELETE", f"/api/passkeys/{keys[0]['id']}", json={})
        assert r.status_code == 200, r.text
        marks = {d["id"]: d["passkey"] for d in srv.devices.list()}
        assert marks[device_id] is False and marks[third] is False, marks
        assert c.get("/api/state").status_code == 428, "a passkey login keeps requiring one"
        print("removal clears the key mark on every device that used it")

        # removing the credential keeps the owner handle, so the app can
        # still signal "accept none" and prune the keychain
        assert passkey.signal_payload()["credential_ids"] == []
        assert passkey.signal_payload()["user_id"], "handle must outlive the credential"
        print("removal keeps the handle for signalling")

        # --- an unreachable origin disables passkeys entirely ---
        passkey.configure("http://192.168.1.10:8780", 8780)
        assert not passkey.available(), "plain-IP origins cannot host passkeys"
        passkey.configure(None, 8780)
        assert passkey.available() and passkey.rp_id() == "localhost"
        print("origin rules ok")

        # --- names and ids: both address a device, neither may shadow the other ---
        free = issue_pairing_code(require_passkey=False)
        c.cookies.clear()
        assert c.post("/api/login", json={"code": free, "name": "mac mini"}).status_code == 200
        assert c.get("/api/state").status_code == 200, "--no-passkey: no gate"
        mac = c.get("/api/passkey").json()["device"]["id"]
        assert srv.devices.resolve("Mac  Mini") == mac and srv.devices.resolve(mac) == mac
        assert srv.devices.name_taken(device_id), "another device's id is not a free name"
        _tok, dev = srv.devices.create(third)   # asks for a name equal to an id
        assert dev["name"] != third and dev["id"] not in {device_id, third, mac}, dev
        # the CLI takes the name too, and only it turns the requirement off
        env = {**os.environ, "C4AI_DATA_DIR": str(DATA_DIR)}
        root = Path(__file__).resolve().parent.parent
        for state in ("on", "off"):
            flag = "--passkey" if state == "on" else "--no-passkey"
            out = subprocess.run([sys.executable, "-m", "clicker4ai", "devices", "set",
                                  "mac mini", flag], cwd=root, env=env,
                                 capture_output=True, text=True)
            assert out.returncode == 0 and "mac mini" in out.stdout, out
            assert c.get("/api/state").status_code == (428 if state == "on" else 200)
        out = subprocess.run([sys.executable, "-m", "clicker4ai", "devices", "lock", "nobody"],
                             cwd=root, env=env, capture_output=True, text=True)
        assert out.returncode != 0 and "No such device" in out.stderr, out
        # the app can only switch it on
        assert c.post("/api/passkey/require", json={}).json()["passkey_required"] is True
        assert c.get("/api/state").status_code == 428
        print("device refs ok — id or name, names never shadow ids, CLI-only off")

    print("\nALL PASSKEY TESTS PASSED")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(DATA_DIR, ignore_errors=True)
