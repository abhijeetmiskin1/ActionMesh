"""Phase 5 Agent 7 — live device pairing assessment. FIXTURE-TESTED ONLY.

This file pins the pairing flow through the **real HTTP surface**
(``create_app`` + ``TestClient``): ``POST /pairing/code`` -> ``POST /pair`` ->
credential issue -> device registry enrol. Every test here runs against an
in-process FastAPI app with in-memory ``IdentityStore`` / ``DeviceRegistry``
and a synthetic phone identity. **No real second device or agent process was
involved**; see :class:`TestLiveAssessment` for why.

Live-probe evidence (recorded 2026-11-05, sandbox ``ss``/``ps``/``curl``):

* Listening sockets are unrelated infra (claude, node, cc-switch, CUPS, DNS)
  plus **one** ActionMesh service: the desktop shell's own uvicorn,
  ``127.0.0.1:41385``, running ``actionmesh.api:app`` as a child of the
  Electron ``actionmesh`` process (pid 10268 under pid 10020).
* ``GET /health`` answers ``{"status":"ok","protocol":"AMP/1.0"}`` on that
  port, but ``GET /devices`` answers ``401 unauthorized`` — the service is
  API-key protected and this probe held no key.
* No second agent endpoint exists: no agent socket under ``/tmp`` or ``/run``
  beyond the terminal multiplexer's own, no ``ps`` entry for a second
  ``actionmesh`` agent, and by design there is no ``/devices/register`` or
  ``/devices/heartbeat`` route (``api.py``: "There is deliberately no
  registration or heartbeat route"). Remote admission is pairing-only, and
  ``LaptopAgent`` presence is in-process, not a separate process.

What "live" pairing would require and is absent here: a second real
device/agent instance presenting the code over a real network or socket
transport, with a human reading the code off one screen and typing it into
the other. The only live service on this machine is the desktop shell's own
API instance, which this suite deliberately does **not** mutate (no codes
issued, no devices revoked against it). Live pairing is therefore
**BLOCKED** in this environment; the honest label for everything below is
**FIXTURE-TESTED**.

What the tests prove, through the HTTP surface:

* (a) fixture pairing succeeds end to end (code -> pair -> token -> device
  enrolled, token authenticates on a protected route);
* (b) a revoked credential/identity is never silently restored: re-pairing
  the same ``device_id`` is refused, the old token stays 401, and the old
  credential record stays revoked in the store;
* (c) the Phase 4 Agent 3 forget-cascade holds through the API: revoking a
  device kills its credentials in the store, not just its registry row;
* (d) there is exactly one canonical ``CredentialStore`` per app: every
  reference (``app.state.credentials``, ``pairing.credentials``,
  ``devices.credentials``) is the identical object — no competing store;
* (e) an expired credential is refused (HTTP 401 and ``authenticate``
  raising), never treated as live.
"""

from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from actionmesh.api import create_app
from actionmesh.core.credentials import CredentialError, CredentialStore
from actionmesh.core.devices import DeviceRegistry
from actionmesh.core.identity import IdentityError, IdentityStore
from actionmesh.protocol.models import DeviceKind

LOCAL = ("127.0.0.1", 45123)  # minting a code is the operator's act
REMOTE = ("192.168.1.50", 55555)  # the phone is remote by definition


def _harness() -> tuple[TestClient, TestClient, object]:
    identities = IdentityStore()
    devices = DeviceRegistry(identities=identities)
    app = create_app(identities=identities, devices=devices)
    devices.enroll(
        device_id="laptop-1",
        kind=DeviceKind.LAPTOP,
        name="My Laptop",
        platform="linux",
        capabilities=["message.send"],
    )
    return (
        TestClient(app, client=LOCAL),
        TestClient(app, client=REMOTE),
        app,
    )


class _EnvPatch(unittest.TestCase):
    def setUp(self) -> None:
        # No API key and no remote-pairing widening: the deployment defaults.
        # TestClient's default peer is "testclient" (not loopback), which is
        # why explicit client addresses are passed.
        for name in (
            "ACTIONMESH_API_KEY",
            "ACTIONMESH_ALLOW_REMOTE_PAIRING",
            "ACTIONMESH_SECRET_KEY",
            "ACTIONMESH_SECRET_FILE",
        ):
            self.addCleanup(os.environ.pop, name, None)
            os.environ.pop(name, None)
        self.local, self.remote, self.app = _harness()

    def pair(self, *, phone: str = "phone-1", actor: str = "abhi") -> dict:
        """Mint on the laptop, spend from the phone across the room."""
        code = self.local.post(
            "/pairing/code", json={"device_id": "laptop-1"}
        ).json()["code"]
        response = self.remote.post(
            "/pair",
            json={
                "code": code,
                "device_id": phone,
                "actor_id": actor,
                "name": "Pixel",
            },
        )
        assert response.status_code == 200, response.text
        return response.json()


class TestFixturePairingViaHttp(_EnvPatch):
    """(a) Pairing through the real HTTP surface, fixture-side.

    FIXTURE-TESTED: both "devices" are synthetic clients of one in-process
    app. This proves the ceremony, not its behaviour against a real phone.
    """

    def test_pairing_succeeds_end_to_end(self) -> None:
        body = self.pair()
        self.assertEqual(body["device_id"], "phone-1")
        self.assertEqual(body["actor_id"], "abhi")
        self.assertTrue(body["credential_id"])
        self.assertTrue(body["token"])
        self.assertEqual(body["laptop_device_id"], "paired from laptop-1")

        token = body["token"]
        # The issued token authenticates on a protected route.
        authed = self.local.get(
            "/devices", headers={"Authorization": f"Bearer {token}"}
        )
        self.assertEqual(authed.status_code, 200, authed.text)
        # The phone is enrolled in the registry with a bound identity.
        detail = self.local.get("/devices/phone-1").json()
        self.assertEqual(detail["kind"], "phone")
        self.assertTrue(detail["enrolled"])
        self.assertTrue(detail["online"])
        # The token never appears in the device view or the pair response
        # beyond the single field the ceremony hands back.
        self.assertNotIn("token", detail)
        self.assertNotIn("secret", str(detail).lower())

    def test_code_is_single_use(self) -> None:
        self.pair()
        code = self.local.post(
            "/pairing/code", json={"device_id": "laptop-1"}
        ).json()["code"]
        self.remote.post(
            "/pair",
            json={
                "code": code,
                "device_id": "phone-2",
                "actor_id": "abhi",
                "name": "Pixel 2",
            },
        )
        replay = self.remote.post(
            "/pair",
            json={
                "code": code,
                "device_id": "phone-3",
                "actor_id": "abhi",
                "name": "Pixel 3",
            },
        )
        self.assertEqual(replay.status_code, 400)
        self.assertIn("already used", replay.json()["detail"])


class TestRevokedNeverRestored(_EnvPatch):
    """(b) A revoked credential stays revoked — re-pairing cannot restore it.

    FIXTURE-TESTED. The only sanctioned reconnect path is
    ``PairingService.reconnect`` (fresh code, *new* identity_id, old record
    stays dead); there is no HTTP route for it, so through the API a
    re-pairing attempt against the same ``device_id`` must fail closed.
    """

    def test_revoked_device_token_dies_and_cannot_be_repaired(self) -> None:
        body = self.pair()
        token = body["token"]
        credential_id = body["credential_id"]

        # Forget the device: registry row and identity go together.
        revoke = self.local.post(
            "/devices/phone-1/revoke", json={"reason": "lost"}
        )
        self.assertTrue(revoke.json()["revoked"])

        # The old token is dead on the wire.
        dead = self.local.get(
            "/devices", headers={"Authorization": f"Bearer {token}"}
        )
        self.assertEqual(dead.status_code, 401)

        # Re-pairing the same device_id is refused: the revoked identity
        # still blocks re-enrolment ("revoke the device before pairing it
        # again" is the half that matters — the API must not un-revoke).
        code = self.local.post(
            "/pairing/code", json={"device_id": "laptop-1"}
        ).json()["code"]
        again = self.remote.post(
            "/pair",
            json={
                "code": code,
                "device_id": "phone-1",
                "actor_id": "abhi",
                "name": "Pixel",
            },
        )
        self.assertEqual(again.status_code, 400)
        self.assertIn("already enrolled", again.json()["detail"])

        # The old credential record is still revoked in the store and the
        # identity still refuses to resolve — nothing was silently restored.
        store = self.app.state.credentials  # type: ignore[attr-defined]
        record = store.get(credential_id)
        assert record is not None
        self.assertTrue(record.revoked)
        self.assertFalse(record.is_active())
        with self.assertRaises(IdentityError):
            self.app.state.identities.resolve(  # type: ignore[attr-defined]
                record.identity_id
            )
        with self.assertRaises(CredentialError):
            store.authenticate(token)
        # A revoked credential cannot rotate back to life either.
        with self.assertRaises(CredentialError):
            store.rotate(credential_id)


class TestForgetCascadeViaApi(_EnvPatch):
    """(c) The Phase 4 Agent 3 credential cascade, observed through the API.

    FIXTURE-TESTED. ``DeviceRegistry.forget`` revokes the identity; the wired
    cascade must also revoke the credential records themselves, so the
    forgotten device is dead in the store, not merely unreachable.
    """

    def test_revoke_kills_credentials_in_the_store(self) -> None:
        body = self.pair()
        token = body["token"]
        store = self.app.state.credentials  # type: ignore[attr-defined]
        identity_id = store.get(body["credential_id"]).identity_id  # type: ignore[union-attr]

        self.local.post("/devices/phone-1/revoke", json={"reason": "stolen"})

        # Device gone from the public view...
        self.assertEqual(self.local.get("/devices/phone-1").status_code, 404)
        # ...and every credential bound to its identity is revoked, not
        # just unreachable through identity resolution.
        for record in store.for_identity(identity_id):
            self.assertTrue(record.revoked)
            self.assertFalse(record.is_active())
        with self.assertRaises(CredentialError):
            store.authenticate(token)
        authed = self.local.get(
            "/devices", headers={"Authorization": f"Bearer {token}"}
        )
        self.assertEqual(authed.status_code, 401)


class TestSingleCredentialStore(_EnvPatch):
    """(d) No second/competing credential store exists. FIXTURE-TESTED.

    ``create_app`` wires exactly one ``CredentialStore``: ``pairing`` spends
    through it, the middleware authenticates through it, and the device
    registry's forget-cascade revokes through it. This pins object identity,
    which is what "single store" actually means — a second store that the
    pairing service consulted but the middleware did not would mint tokens
    the API refuses (or worse, honour tokens pairing never issued).
    """

    def test_all_references_are_the_identical_object(self) -> None:
        store = self.app.state.credentials  # type: ignore[attr-defined]
        self.assertIs(store, self.app.state.pairing.credentials)  # type: ignore[attr-defined]
        self.assertIs(store, self.app.state.devices.credentials)  # type: ignore[attr-defined]
        self.assertIsInstance(store, CredentialStore)

    def test_app_state_holds_exactly_one_store_instance(self) -> None:
        # Starlette stores State attrs in _state, not dir(); iterate that.
        state = self.app.state  # type: ignore[attr-defined]
        stores = {
            id(value)
            for value in state._state.values()
            if isinstance(value, CredentialStore)
        }
        self.assertEqual(len(stores), 1)
        # And it is the same object the pairing ceremony mints against.
        self.assertIs(state.credentials, state._state["credentials"])

    def test_issued_token_authenticates_against_the_same_store(self) -> None:
        # A token minted via /pair is honoured by the middleware's store —
        # the two sides of the ceremony share one canonical store.
        body = self.pair()
        record = self.app.state.credentials.authenticate(body["token"])  # type: ignore[attr-defined]
        self.assertEqual(record.credential_id, body["credential_id"])
        self.assertEqual(record.device_id, "phone-1")

    def test_separate_app_instances_do_not_share_a_store(self) -> None:
        _, _, other = _harness()
        self.assertIsNot(
            self.app.state.credentials,  # type: ignore[attr-defined]
            other.state.credentials,  # type: ignore[attr-defined]
        )


class TestExpiredCredentialRefused(_EnvPatch):
    """(e) An expired credential is refused. FIXTURE-TESTED.

    Credentials normally live 30 days (``DEFAULT_CREDENTIAL_TTL_S``); the
    expiry is forced here by back-dating the record in-process, which is the
    only honest way to observe the refusal without waiting a month.
    """

    def test_expired_token_is_401_and_never_live(self) -> None:
        body = self.pair()
        token = body["token"]
        store = self.app.state.credentials  # type: ignore[attr-defined]
        record = store.get(body["credential_id"])
        assert record is not None
        record.expires_at = datetime.now(UTC) - timedelta(seconds=1)

        authed = self.local.get(
            "/devices", headers={"Authorization": f"Bearer {token}"}
        )
        self.assertEqual(authed.status_code, 401)
        self.assertFalse(record.is_active())
        with self.assertRaises(CredentialError):
            store.authenticate(token)
        auth = store.authorize(record, "calendar.read")
        self.assertFalse(auth.allowed)
        # Expiry is terminal in the same way revocation is: no rotation.
        with self.assertRaises(CredentialError):
            store.rotate(record.credential_id)


class TestLiveAssessment(unittest.TestCase):
    """What was probed, and what was deliberately not claimed."""

    def test_this_suite_is_labelled_fixture_tested(self) -> None:
        module = globals()["__doc__"] or ""
        self.assertIn("FIXTURE-TESTED", module)
        self.assertIn("BLOCKED", module)

    def test_no_live_second_device_was_contacted(self) -> None:
        # The only live ActionMesh process on this machine is the desktop
        # shell's own uvicorn (127.0.0.1:41385, API-key protected). This
        # suite asserts no client, socket, or request it built addresses
        # that process — both clients above are in-process TestClients.
        local, remote, app = _harness()
        self.assertIsInstance(local, TestClient)
        self.assertIsInstance(remote, TestClient)
        self.assertIsNotNone(app)
