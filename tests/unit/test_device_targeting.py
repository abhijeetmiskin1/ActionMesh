"""C36 — "which computer did you mean?", and refusing to guess when it is unclear.

The work order for multi-device control says, in four different ways, the same thing:
*never silently act on the wrong device.*  So the tests here are about the two failures
that would produce exactly that, and the two behaviours that prevent them:

* **matching is conservative.** A computer is named by its own id, its own name, or its
  platform/role on an *explicit* switch. A word that merely appears somewhere in an
  ordinary sentence ("show my linux processes") is not a target -- otherwise "show the
  linux processes" would run on a Linux workstation the user never mentioned.
* **a tie is a question.** Two laptops called "MacBook", or "the laptop" when two are
  registered, produces candidates and a sentence asking which; it never produces the
  first one.
* **an offline target is reported as offline.** It is not quietly redirected, and the
  default target never picks one.
* **the targeting layer has no authority.** It hands the executor an id; the executor
  re-resolves it, checks role and ceiling, and refuses. A resolved name is a routing
  request, and the tests that matter for the last part live in
  :mod:`tests.unit.test_device_routing` -- this module only proves the name was right.
"""

from __future__ import annotations

import unittest

from actionmesh.core.devices import DeviceRegistry
from actionmesh.core.identity import IdentityStore
from actionmesh.protocol.models import DeviceKind, RiskLevel
from actionmesh.voiceos.models import Conversation, TaskSession, TaskStep
from actionmesh.voiceos.reasoning import Reasoner
from actionmesh.voiceos.targeting import (
    current,
    default_target,
    describe_choice,
    executable,
    resolve,
)


def _registry() -> DeviceRegistry:
    """Two laptops and a phone; nothing enrolled has a heartbeat yet."""
    registry = DeviceRegistry(identities=IdentityStore())
    registry.enroll(
        device_id="dev_linux",
        kind=DeviceKind.LAPTOP,
        name="Linux Workstation",
        platform="linux",
        arch="x86_64",
        capabilities=["device.process.list"],
        risk_ceiling=RiskLevel.HIGH,
    )
    registry.enroll(
        device_id="dev_mac",
        kind=DeviceKind.LAPTOP,
        name="MacBook Pro",
        platform="macos",
        arch="arm64",
        capabilities=["device.process.list"],
        risk_ceiling=RiskLevel.HIGH,
    )
    registry.enroll(
        device_id="phone_1",
        kind=DeviceKind.PHONE,
        name="Pixel",
        platform="android",
    )
    return registry


def _online(registry: DeviceRegistry, *device_ids: str) -> DeviceRegistry:
    for device_id in device_ids:
        registry.heartbeat(device_id)
    return registry


class TestNamingAComputer(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = _registry()

    def test_a_full_name_resolves_to_exactly_that_device(self) -> None:
        choice = resolve(self.registry, "run the tests on my Linux Workstation")
        self.assertTrue(choice.resolved)
        self.assertEqual(choice.device_id, "dev_linux")
        self.assertEqual(choice.name, "Linux Workstation")

    def test_the_device_id_is_a_name_too(self) -> None:
        choice = resolve(self.registry, "restart dev_mac")
        self.assertEqual(choice.device_id, "dev_mac")

    def test_a_platform_word_names_a_computer_only_when_switching(self) -> None:
        """Two vocabularies, one function.

        "switch to the mac" is an explicit pick and should work. "show my mac
        processes" is an observation, and treating its descriptive word as a target
        would run it somewhere the user never named.
        """
        self.assertFalse(resolve(self.registry, "show my mac processes").resolved)
        chosen = resolve(self.registry, "the mac", phrases=True)
        self.assertEqual(chosen.device_id, "dev_mac")

    def test_a_platform_word_alone_is_not_a_name_inside_a_plain_request(self) -> None:
        """This is the wrong-computer failure: "linux" must not mean the Linux box."""
        choice = resolve(self.registry, "show my linux processes")
        self.assertFalse(choice.resolved, "'linux' is a word, not a computer, here")

    def test_naming_a_phone_says_it_is_a_control_surface(self) -> None:
        choice = resolve(self.registry, "run it on the Pixel")
        self.assertFalse(choice.resolved)
        self.assertIn("control surface", choice.reason)
        self.assertTrue(choice.named, "the phone *was* named; that is why it is refused")

    def test_no_name_in_the_request_is_reported_as_no_name(self) -> None:
        choice = resolve(self.registry, "what is running")
        self.assertFalse(choice.resolved)
        self.assertFalse(choice.named)
        self.assertIn("no registered computer is named", choice.reason)

    def test_two_matching_names_produce_candidates_not_a_coin_toss(self) -> None:
        registry = _registry()
        registry.enroll(
            device_id="dev_mac_2",
            kind=DeviceKind.LAPTOP,
            name="MacBook Pro",
            platform="macos",
        )
        choice = resolve(registry, "stop the server on MacBook Pro")
        self.assertFalse(choice.resolved)
        self.assertEqual(
            {device_id for device_id, _ in choice.candidates},
            {"dev_mac", "dev_mac_2"},
        )
        self.assertIn("Which computer did you mean", describe_choice(choice))

    def test_a_suffix_of_a_name_is_not_a_name(self) -> None:
        """A device called "dev" must not be named by "development"."""
        registry = DeviceRegistry(identities=IdentityStore())
        registry.enroll(device_id="x", kind=DeviceKind.LAPTOP, name="dev", platform="linux")
        self.assertFalse(resolve(registry, "start the development server").resolved)

    def test_an_offline_device_is_named_and_reported_offline(self) -> None:
        """Naming a sleeping machine is answered honestly; it is not re-routed."""
        _online(self.registry, "dev_linux")  # the Mac stays asleep
        choice = resolve(self.registry, "run it on the MacBook Pro")
        self.assertTrue(choice.resolved)
        self.assertFalse(choice.online)
        self.assertIn("offline", describe_choice(choice))


class TestWhatTheDefaultTargetIs(unittest.TestCase):
    def test_the_served_machine_is_the_default(self) -> None:
        registry = _online(_registry(), "dev_mac")
        choice = default_target(registry, "dev_linux")
        self.assertEqual(choice.device_id, "dev_linux")
        self.assertFalse(choice.online, "it was not heartbeating; the default says so")

    def test_one_online_computer_is_the_default(self) -> None:
        registry = _online(_registry(), "dev_mac")
        choice = default_target(registry, "")
        self.assertEqual(choice.device_id, "dev_mac")
        self.assertTrue(choice.online)

    def test_several_online_computers_are_a_question(self) -> None:
        registry = _online(_registry(), "dev_linux", "dev_mac")
        choice = default_target(registry, "")
        self.assertFalse(choice.resolved)
        self.assertEqual(len(choice.candidates), 2)
        self.assertEqual(choice.reason, "several computers are online")

    def test_no_online_computer_never_defaults_to_an_offline_one(self) -> None:
        choice = default_target(_registry(), "")
        self.assertFalse(choice.resolved)
        self.assertIn("no registered computer is online", choice.reason)
        self.assertIn("Linux Workstation", choice.reason)

    def test_a_phone_is_never_an_execution_target(self) -> None:
        registry = _online(_registry(), "phone_1")
        choice = default_target(registry, "phone_1")
        self.assertFalse(choice.resolved)
        self.assertIn("no registered computer is online", choice.reason)
        self.assertEqual([device.device_id for device in executable(registry)],
                         ["dev_linux", "dev_mac"])


class TestAStaleOrExplicitSelection(unittest.TestCase):
    def test_a_revoked_selection_is_reported_not_replaced(self) -> None:
        registry = _registry()
        choice = current(registry, "dev_gone")
        self.assertFalse(choice.resolved)
        self.assertIn("no longer registered", choice.reason)

    def test_a_selection_is_never_swapped_for_the_first_device(self) -> None:
        registry = _online(_registry(), "dev_linux")
        self.assertFalse(current(registry, "dev_mac_2").resolved)
        self.assertEqual(current(registry, "").device_id, "")


class _StubPlanner:
    def plan(self, text: str):
        from actionmesh.core.planner import Plan, ProposedAction

        return Plan(
            intent=text,
            actions=(ProposedAction(type="device.process.list", parameters={}),),
        )


class TestSwitchingChangesTheAddressAndNothingElse(unittest.TestCase):
    """The reasoner resolves the *word*; the executor still holds the authority."""

    def setUp(self) -> None:
        self.registry = _online(_registry(), "dev_linux", "dev_mac")
        self.conversation = Conversation(actor_id="voice-user")
        self.reasoner = self._reasoner()

    def _reasoner(self, device_id: str = "") -> Reasoner:
        return Reasoner(
            planner=_StubPlanner(),
            known_action=lambda name: name.startswith("device."),
            templates=lambda: (),
            device_id=device_id,
            resolve=lambda name: resolve(self.registry, name, phrases=True),
        )

    def test_switch_to_a_named_computer_selects_it(self) -> None:
        answer = self.reasoner.understand("switch to the Linux Workstation", self.conversation)
        self.assertEqual(answer.kind, "device")
        self.assertIsNotNone(answer.target)
        self.assertEqual(answer.target.device_id, "dev_linux")
        self.assertIn("Linux Workstation", answer.reply)

    def test_switching_to_an_offline_computer_says_so_in_the_same_breath(self) -> None:
        self.registry.devices["dev_mac"].online = False
        answer = self.reasoner.understand("switch to the MacBook Pro", self.conversation)
        self.assertEqual(answer.kind, "device")
        self.assertIn("offline", answer.reply)
        self.assertIn("will be refused", answer.reply)

    def test_switching_to_a_name_it_cannot_place_asks_instead_of_picking(self) -> None:
        answer = self.reasoner.understand("switch to the toaster", self.conversation)
        self.assertEqual(answer.kind, "answer")
        self.assertIsNone(answer.session)
        self.assertIn("no registered computer is named", answer.reply)
        self.assertTrue(answer.unresolved)

    def test_a_selected_computer_is_written_onto_the_proposed_steps(self) -> None:
        reasoner = self._reasoner("dev_mac")
        answer = reasoner.understand("tell mom I'll be late", self.conversation)
        self.assertEqual(answer.kind, "task")
        self.assertEqual(answer.session.device_id, "dev_mac")
        self.assertEqual({step.device_id for step in answer.session.steps}, {"dev_mac"})

    def test_a_step_that_names_its_own_computer_keeps_it(self) -> None:
        """An operator-authored multi-device plan must not be rewritten by a switch."""
        session = TaskSession(session_id="t", actor_id="voice-user", request="x")
        step = TaskStep(index=0, label="a", action_type="device.process.list",
                        device_id="dev_linux")
        session.steps.append(step)
        reasoner = self._reasoner("dev_mac")
        built = reasoner._session("x", [step], self.conversation)
        self.assertEqual(built.steps[0].device_id, "dev_linux")
        self.assertEqual(session.steps[0].device_id, "dev_linux")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()