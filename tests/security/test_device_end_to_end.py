"""C19 end to end: a phone asks, the laptop does it for real, and the answer is verified.

The definition-of-done sequence in one test file:

    phone request → ActionMesh → policy → approval → LaptopAgent → real process on the
    real filesystem → verification → audit → result back on the phone

Every step here runs against a real temporary directory on a real filesystem. The
assertions that matter are not "the API returned 200" but "the bytes are on disk" and
"the audit says who did it", because a control plane that reports success without
either of those is the thing this project exists to avoid.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import socket
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from actionmesh.agent.config import (
    ENV_ALLOW_SHELL,
    ENV_DEVICE_ID,
    ENV_ENABLED,
    ENV_WORKSPACE_ROOT,
)
from actionmesh.api import create_app
from actionmesh.core.devices import DeviceRegistry
from actionmesh.core.identity import IdentityStore

LOCAL = ("127.0.0.1", 45123)
REMOTE = ("10.0.0.42", 51111)
ACTOR = "abhi"


def free_port() -> int:
    """An OS-assigned port, so no two tests in one run can collide."""
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class TestPhoneControlsLaptop(unittest.TestCase):
    def setUp(self) -> None:
        for name in (
            ENV_ENABLED,
            ENV_WORKSPACE_ROOT,
            ENV_DEVICE_ID,
            ENV_ALLOW_SHELL,
            "ACTIONMESH_API_KEY",
            "ACTIONMESH_ALLOW_REMOTE_PAIRING",
            "ACTIONMESH_SECRET_KEY",
            "ACTIONMESH_SECRET_FILE",
        ):
            self.addCleanup(os.environ.pop, name, None)
            os.environ.pop(name, None)
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        with open(os.path.join(self.root, "README.md"), "w", encoding="utf-8") as handle:
            handle.write("# ActionMesh\n")

        os.environ[ENV_ENABLED] = "1"
        os.environ[ENV_WORKSPACE_ROOT] = self.root
        os.environ[ENV_DEVICE_ID] = "laptop-1"
        # The operator opened the terminal boundary. Without this the command
        # capabilities are never registered at all -- see
        # test_a_terminal_capability_that_was_not_configured_is_not_an_action.
        os.environ[ENV_ALLOW_SHELL] = "1"
        os.environ["ACTIONMESH_ALLOWED_COMMANDS"] = "git,python3"
        identities = IdentityStore()
        self.app = create_app(
            identities=identities, devices=DeviceRegistry(identities=identities)
        )
        # Entered as context managers so all requests share ONE portal, and therefore
        # one event loop -- which is the shape a real server has. A TestClient used
        # without its context opens a fresh loop per request, so a child started by
        # one request can never be awaited by the next, and the stop path falls back
        # to the raw kill. Testing that fallback is worthwhile; testing nothing else
        # through it is not.
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.laptop = self.stack.enter_context(
            TestClient(self.app, client=LOCAL)
        )
        self.phone = self.stack.enter_context(
            TestClient(self.app, client=REMOTE)
        )

    # -- ceremony ---------------------------------------------------------
    def pair(self, max_risk: str = "medium") -> str:
        """The ceremony: the laptop chooses the ceiling, the phone spends the code."""
        grant = self.laptop.post(
            "/consents",
            json={
                "actor_id": ACTOR,
                "scope": "device.control",
                "max_risk": "critical",
                "reason": "fixture consent before pairing",
            },
        )
        self.assertEqual(grant.status_code, 200, grant.text)
        code = self.laptop.post(
            "/pairing/code", json={"device_id": "laptop-1", "max_risk": max_risk}
        ).json()["code"]
        paired = self.phone.post(
            "/pair",
            json={"code": code, "device_id": "phone-1", "actor_id": ACTOR, "name": "Pixel"},
        )
        self.assertEqual(paired.status_code, 200, paired.text)
        return paired.json()["token"]

    def execute(self, token: str, action_type: str, parameters: dict) -> dict:
        response = self.phone.post(
            "/execute",
            json={
                "graph": {
                    "nodes": [
                        {
                            "node_id": "one",
                            "action": {"type": action_type, "parameters": parameters},
                        },
                        {"node_id": "end", "node_type": "end", "depends_on": ["one"]},
                    ]
                },
                "policy": {"rules": [{"action": action_type, "decision": "allow"}]},
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def approve(self, result: dict) -> dict:
        """Approve the one action a `waiting_permission` result asked about."""
        waiting = self.laptop.get("/approvals").json()
        action_id = next(
            item["action_id"]
            for item in waiting[0]["actions"]
            if item["action_id"] == result["nodes"]["one"]["action_id"]
        )
        decided = self.laptop.post(
            "/approvals",
            json={
                "action_id": action_id,
                "status": "approved",
                "execution_id": waiting[0]["execution_id"],
            },
        )
        self.assertEqual(decided.status_code, 200, decided.text)
        return decided.json()["execution"]

    def consent(self, scope: str, max_risk: str = "critical") -> None:
        response = self.laptop.post(
            "/consents",
            json={"actor_id": ACTOR, "scope": scope, "max_risk": max_risk},
        )
        self.assertEqual(response.status_code, 200, response.text)

    # -- the story --------------------------------------------------------
    def test_the_laptop_is_present_before_the_phone_arrives(self) -> None:
        listed = self.laptop.get("/devices").json()
        self.assertEqual([d["device_id"] for d in listed], ["laptop-1"])
        self.assertTrue(listed[0]["online"])
        self.assertEqual(listed[0]["kind"], "laptop")
        self.assertIn("device.file.read", listed[0]["capabilities"])

    def test_a_paired_phone_can_read_a_real_file(self) -> None:
        token = self.pair("medium")
        self.consent("file.read")
        result = self.execute(token, "device.file.read", {"path": "README.md"})
        self.assertEqual(result["state"], "success", result)
        node = result["nodes"]["one"]
        self.assertTrue(node["verified"])
        # The one flag that says "this was the machine, not a simulation".
        self.assertFalse(node["simulated"])
        self.assertFalse(result["simulated"])

    def test_an_unpaired_client_cannot_read_anything(self) -> None:
        self.consent("file.read")
        response = self.phone.post(
            "/execute",
            json={
                "graph": {"nodes": []},
                "policy": {"rules": [{"action": "device.file.read", "decision": "allow"}]},
            },
            headers={"Authorization": "Bearer not-a-token-at-all"},
        )
        # Rejected at the boundary: nothing is dispatched, so nothing is audited as an
        # execution either.
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "unauthorized")

    def test_a_paired_phone_still_needs_consent_for_every_scope(self) -> None:
        token = self.pair()
        # No consent recorded for file.read: pairing alone authorizes nothing.
        result = self.execute(token, "device.file.read", {"path": "README.md"})
        self.assertEqual(result["state"], "denied")

    def test_a_default_pairing_reaches_nothing_above_low_risk(self) -> None:
        # Pairing at the default ceiling cannot read a MEDIUM-risk file, however much
        # consent the actor holds: the credential narrows, never widens.
        token = self.pair("low")
        self.consent("file.read")
        result = self.execute(token, "device.file.read", {"path": "README.md"})
        self.assertEqual(result["state"], "denied", result)

    def test_a_high_risk_write_waits_for_approval_then_lands_on_disk(self) -> None:
        token = self.pair("high")
        self.consent("file.write")
        first = self.execute(
            token, "device.file.write", {"path": "notes.txt", "content": "from the phone\n"}
        )
        # HIGH risk over HTTP cannot auto-allow: `allow_high_risk` is server-side only.
        self.assertEqual(first["state"], "waiting_permission", first)
        action_id = first["nodes"]["one"]["action_id"]
        self.assertTrue(action_id)
        self.assertFalse(os.path.exists(os.path.join(self.root, "notes.txt")))

        waiting = self.laptop.get("/approvals").json()
        self.assertEqual(len(waiting), 1)
        self.assertIn(action_id, [item["action_id"] for item in waiting[0]["actions"]])

        decided = self.laptop.post(
            "/approvals",
            json={
                "action_id": action_id,
                "action_type": "device.file.write",
                "status": "approved",
                "execution_id": waiting[0]["execution_id"],
            },
        )
        self.assertEqual(decided.status_code, 200, decided.text)
        body = decided.json()
        self.assertEqual(body["execution"]["state"], "success", body)

        # The bytes are really there.
        with open(os.path.join(self.root, "notes.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "from the phone\n")

    def test_a_rejected_approval_leaves_nothing_written(self) -> None:
        token = self.pair("high")
        self.consent("file.write")
        first = self.execute(token, "device.file.write", {"path": "no.txt", "content": "x"})
        waiting = self.laptop.get("/approvals").json()
        self.phone.post(
            "/approvals",
            json={
                "action_id": first["nodes"]["one"]["action_id"],
                "status": "rejected",
                "execution_id": waiting[0]["execution_id"],
            },
        )
        still = self.execute(token, "device.file.write", {"path": "no.txt", "content": "x"})
        self.assertEqual(still["state"], "waiting_permission")
        self.assertFalse(os.path.exists(os.path.join(self.root, "no.txt")))

    def test_an_approval_for_the_wrong_action_does_not_unlock_this_one(self) -> None:
        token = self.pair("high")
        self.consent("file.write")
        first = self.execute(token, "device.file.write", {"path": "x.txt", "content": "x"})
        self.assertEqual(first["state"], "waiting_permission")
        waiting = self.laptop.get("/approvals").json()
        response = self.laptop.post(
            "/approvals",
            json={
                "action_id": "act_someone_elses",
                "status": "approved",
                "execution_id": waiting[0]["execution_id"],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["execution"]["state"], "waiting_permission")

    def test_resuming_an_execution_that_is_not_waiting_is_refused(self) -> None:
        self.consent("file.write")
        response = self.laptop.post(
            "/approvals",
            json={"action_id": "act_x", "status": "approved", "execution_id": "exec_ghost"},
        )
        self.assertEqual(response.status_code, 409)

    def test_the_phone_can_run_a_real_command_after_approval(self) -> None:
        """The DoD sequence, with a real program instead of a test double."""
        token = self.pair("critical")
        self.consent("system.shell", max_risk="critical")
        first = self.execute(token, "device.terminal.execute", {"command": "git", "arguments": ["--version"]})
        # CRITICAL risk always asks. Nothing ran yet.
        self.assertEqual(first["state"], "waiting_permission", first)
        waiting = self.laptop.get("/approvals").json()
        decided = self.laptop.post(
            "/approvals",
            json={
                "action_id": first["nodes"]["one"]["action_id"],
                "action_type": "device.terminal.execute",
                "status": "approved",
                "execution_id": waiting[0]["execution_id"],
            },
        )
        self.assertEqual(decided.status_code, 200, decided.text)
        body = decided.json()["execution"]
        self.assertEqual(body["state"], "success", body)
        node = body["nodes"]["one"]
        self.assertTrue(node["verified"])
        self.assertFalse(node["simulated"])

    def test_a_phone_cannot_run_a_program_the_laptop_does_not_allow(self) -> None:
        token = self.pair("critical")
        self.consent("system.shell", max_risk="critical")
        first = self.execute(token, "device.terminal.execute", {"command": "rm", "arguments": ["-rf", "/tmp/nope"]})
        waiting = self.laptop.get("/approvals").json()
        decided = self.laptop.post(
            "/approvals",
            json={
                "action_id": first["nodes"]["one"]["action_id"],
                "status": "approved",
                "execution_id": waiting[0]["execution_id"],
            },
        )
        # Approved by the policy engine, refused by the laptop. Authorization is not
        # capability: the human saying yes cannot install a program that is not there.
        body = decided.json()["execution"]
        self.assertEqual(body["state"], "failed", body)
        self.assertIn("refused regardless of the allowlist", body["nodes"]["one"]["error"])
        self.assertFalse(os.path.exists("/tmp/nope"))

    def test_a_terminal_request_cannot_reach_the_shell(self) -> None:
        token = self.pair("critical")
        self.consent("system.shell", max_risk="critical")
        for command, args in (
            ("sh", ["-c", "touch /tmp/actionmesh-shell"]),
            ("bash", ["-c", "id"]),
            ("python3", ["-c", "open('/tmp/actionmesh-inline','w')"]),
            ("git", ["-c", "core.pager=touch /tmp/actionmesh-pager", "log"]),
        ):
            with self.subTest(command=command):
                result = self.execute(token, "device.terminal.execute", {"command": command, "arguments": args})
                if result["state"] == "waiting_permission":
                    waiting = self.laptop.get("/approvals").json()
                    result = self.phone.post(
                        "/approvals",
                        json={
                            "action_id": result["nodes"]["one"]["action_id"],
                            "status": "approved",
                            "execution_id": waiting[0]["execution_id"],
                        },
                    ).json().get("execution", result)
                self.assertNotEqual(result.get("state"), "success", result)
        for path in ("/tmp/actionmesh-shell", "/tmp/actionmesh-inline", "/tmp/actionmesh-pager"):
            self.assertFalse(os.path.exists(path), path)

    def test_the_phone_can_start_and_stop_a_real_server(self) -> None:
        """A dev server, launched from the phone, verified by asking its port."""
        import socket

        token = self.pair("high")
        self.consent("device.server.control", max_risk="critical")
        port = free_port()
        first = self.execute(
            token,
            "device.server.start",
            {
                "name": "api",
                "command": "python3",
                "arguments": ["-m", "http.server", str(port), "--bind", "127.0.0.1"],
                "port": port,
            },
        )
        self.assertEqual(first["state"], "waiting_permission", first)
        body = self.approve(first)
        self.assertEqual(body["state"], "success", body)
        node = body["nodes"]["one"]
        # Verified by the process table; the launch result carries the socket probe.
        self.assertTrue(node["verified"])
        self.assertFalse(node["simulated"])
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass

        stopped = self.execute(token, "device.server.stop", {"name": "api"})
        # A second HIGH action asks again: approval is matched on action_id, so yes to
        # starting is not yes to everything that follows.
        self.assertEqual(stopped["state"], "waiting_permission", stopped)
        final = self.approve(stopped)
        self.assertEqual(final["state"], "success", final)
        # Asking for status afterwards is both the natural phone behaviour and the
        # agent's chance to close the transport of a child it has already killed. It
        # needs its own consent: `device.server.read` is a different scope from the
        # control scope that let the server be started.
        self.assertEqual(
            self.execute(token, "device.server.status", {"name": "api"})["state"],
            "denied",
        )
        # A ceiling of "low" would not cover it: an action's effective risk defaults
        # to MEDIUM, so a LOW-capped consent cannot authorize it. Fail-closed, and
        # worth knowing when granting consent from a phone.
        self.consent("device.server.read")
        status = self.execute(token, "device.server.status", {"name": "api"})
        self.assertEqual(status["state"], "success", status)
        # The kill is delivered, but the kernel closes the listening socket a moment
        # later, so this polls rather than asserting the instant after. A dashboard that
        # read the port once would report a server still up for a few milliseconds.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    time.sleep(0.05)
            except OSError:
                break
        else:
            self.fail("the server was still accepting connections after the stop")
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()

    # -- the boundary holds under attack ----------------------------------
    def test_a_phone_cannot_read_outside_the_workspace(self) -> None:
        token = self.pair()
        self.consent("file.read")
        before = self._passwd_snapshot()
        for path in ("../../etc/passwd", "/etc/passwd", "README.md/../../../../etc/passwd"):
            with self.subTest(path=path):
                result = self.execute(token, "device.file.read", {"path": path})
                self.assertIn(result["state"], ("denied", "failed", "requires_user"), result)
        self.assertEqual(before, self._passwd_snapshot())

    def test_a_phone_cannot_write_outside_the_workspace(self) -> None:
        token = self.pair()
        self.consent("file.write")
        for path in ("../escaped.txt", "/tmp/actionmesh-escape.txt", "src/../../x"):
            with self.subTest(path=path):
                self.execute(token, "device.file.write", {"path": path, "content": "x"})
        self.assertFalse(os.path.exists(os.path.join(self.root, "..", "escaped.txt")))
        self.assertFalse(os.path.exists("/tmp/actionmesh-escape.txt"))

    def test_a_symlink_inside_the_root_does_not_become_a_read_of_the_host(self) -> None:
        token = self.pair()
        self.consent("file.read")
        os.symlink("/etc/passwd", os.path.join(self.root, "innocent.txt"))
        result = self.execute(token, "device.file.read", {"path": "innocent.txt"})
        self.assertNotEqual(result["state"], "success", result)

    def test_a_terminal_capability_that_was_not_configured_is_not_an_action(self) -> None:
        # With the shell boundary closed, the action is not registered, so it fails
        # closed at the registry rather than reaching a policy decision or a device.
        os.environ.pop(ENV_ALLOW_SHELL, None)
        identities = IdentityStore()
        quiet = TestClient(
            create_app(identities=identities, devices=DeviceRegistry(identities=identities)),
            client=REMOTE,
        )
        result = quiet.post(
            "/execute",
            json={
                "graph": {
                    "nodes": [
                        {
                            "node_id": "one",
                            "action": {
                                "type": "device.terminal.execute",
                                "parameters": {"command": "git"},
                            },
                        },
                        {"node_id": "end", "node_type": "end", "depends_on": ["one"]},
                    ]
                },
                "actor": {"id": ACTOR},
                "policy": {
                    "rules": [
                        {"action": "device.terminal.execute", "decision": "allow"}
                    ]
                },
            },
        ).json()
        self.assertEqual(result["state"], "denied", result)
        self.assertIn("unknown action", result["error"])

    def test_a_denied_action_is_audited_with_its_reason(self) -> None:
        token = self.pair()
        self.execute(token, "device.file.read", {"path": "README.md"})  # no consent given
        audit = self.laptop.get("/audit", params={"limit": 300}).json()
        self.assertTrue(audit["chain"]["ok"], audit["chain"])
        # A whole-graph denial stops before any node dispatches, so the record is the
        # denial event itself — and it carries the reason, not just a node id.
        denials = [
            record
            for record in audit["records"]
            if record["result"] == "execution.denied"
        ]
        self.assertTrue(denials, "a denied action left no audit entry")
        error = denials[-1]["payload"]["data"]["error"]
        self.assertIn("not authorized", error)
        self.assertIn("permission", error.lower())

    def test_an_allowed_decision_is_audited(self) -> None:
        token = self.pair("medium")
        self.consent("file.read")
        self.execute(token, "device.file.read", {"path": "README.md"})
        records = self.laptop.get("/audit", params={"limit": 300}).json()["records"]
        allowed = [
            record
            for record in records
            if record["result"] == "authorization.decided"
            # Enum values are the protocol's lowercase forms.
            and record["payload"]["data"]["decision"] == "allow"
        ]
        self.assertTrue(allowed)
        self.assertTrue(allowed[-1]["payload"]["data"]["authorizes_execution"])
        self.assertEqual(allowed[-1]["payload"]["data"]["effective_risk"], "medium")

    def test_the_audit_records_which_principal_and_which_device(self) -> None:
        token = self.pair()
        self.consent("file.read")
        self.execute(token, "device.file.read", {"path": "README.md"})
        audit = self.laptop.get("/audit", params={"limit": 300}).json()
        pairing = [
            record
            for record in audit["records"]
            if record["result"] == "pairing.completed"
        ]
        self.assertTrue(pairing)
        payload = pairing[-1]["payload"]
        # §10: the four principals have to be distinguishable in the record.
        self.assertEqual(payload["actor_id"], ACTOR)
        self.assertEqual(payload["laptop_device_id"], "laptop-1")
        self.assertEqual(payload["identity_id"], self._phone_identity())
        self.assertNotIn("code", [key for key in payload if key == "code"])

    def test_revoking_the_phone_ends_the_story(self) -> None:
        token = self.pair()
        self.consent("file.read")
        self.assertEqual(
            self.execute(token, "device.file.read", {"path": "README.md"})["state"], "success"
        )
        self.laptop.post("/devices/phone-1/revoke", json={"reason": "lost"})
        response = self.phone.post(
            "/execute",
            json={"graph": {"nodes": []}},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(response.status_code, 401)

    # -- helpers ----------------------------------------------------------
    def _passwd_snapshot(self) -> bytes:
        with open("/etc/passwd", "rb") as handle:
            return handle.read()

    def _phone_identity(self) -> str:
        for device in self.app.state.devices.all_devices():  # type: ignore[attr-defined]
            if device.device_id == "phone-1":
                return device.identity_id
        self.fail("phone-1 was never registered")
        return ""


if __name__ == "__main__":
    unittest.main()
