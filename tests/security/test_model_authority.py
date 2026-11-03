"""C41: model output has no authority — proven at the seam, not asserted in prose.

A model is an untrusted input source. It may say what it *proposes* and nothing else:
no risk level, no permission, no grant, no approval, no actor, no device, no policy.
This file pins that at both doors a model answer has to pass through:

* :func:`~actionmesh.voiceos.llm.validate_proposal` — the first door, where an answer
  is stripped to ``{type, parameters}`` or dropped whole;
* the registry/authorization path — the second door, where a *registered* action's
  parameters are checked against that action's own schema before it is dispatched.

The second door is the one that matters most. A proposal can only name a registered
action type, so the interesting attack is not "invent a capability" but "smuggle a
parameter into a capability that already exists" — ``message.send`` with an extra
``command`` key. The planner is *expected* to pass that through; the backend is
expected to refuse it.

No network and no real bridge: the provider is a stub that returns a literal object.
"""

from __future__ import annotations

import json
import unittest
from typing import Any

from actionmesh.core.graph.authorization import GraphAuthorizationError, authorize_graph
from actionmesh.core.graph.serialization import graph_from_dict
from actionmesh.core.permissions import PermissionGrant
from actionmesh.core.permissions.context import build_context
from actionmesh.core.permissions.engine import PolicyEngine
from actionmesh.core.planner import Plan, RuleBasedPlanner
from actionmesh.core.registry import ActionRegistry
from actionmesh.protocol.graphs import validate_plan
from actionmesh.protocol.models import Identity, Policy, PolicyDecision, PolicyRule, RiskLevel
from actionmesh.protocol.validate import ValidationError
from actionmesh.voiceos.llm import LlmError, LlmPlanner, validate_proposal

#: Action types that would hand a model a shell if any of them ever resolved. None is
#: a seed, and the agent's device capabilities are only registered when an agent is
#: configured — so on this registry they must all be unknown.
SHELLISH = ("shell.exec", "run_command", "bash", "sh", "exec")

class _StubProvider:
    """A model that says exactly one thing. No transport, no network, no bridge."""

    def __init__(self, answer: Any, name: str = "stub") -> None:
        self._answer = answer
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def propose(self, request: str) -> Any:
        return self._answer

def _planner(answer: Any, registry: ActionRegistry | None = None) -> LlmPlanner:
    registry = registry or ActionRegistry.with_seeds()
    return LlmPlanner(
        provider=_StubProvider(answer),
        fallback=RuleBasedPlanner(known_action=registry.has),
        known_action=registry.has,
    )

def _payload_text(plan: Plan) -> str:
    return json.dumps(plan.as_payload(), sort_keys=True)

#: Every field a model might assert that it does not own. Any one of them on an action
#: entry must drop that entry whole — not be ignored while the rest is kept.
AUTHORITY_FIELDS = (
    "risk_level",
    "permissions",
    "grant",
    "approved",
    "actor",
    "device_id",
    "policy",
)

class ModelAuthorityTest(unittest.TestCase):
    """What a proposal is allowed to be, and what the backend must refuse."""

    # -- first door: the proposal validator ---------------------------------

    def test_an_action_entry_asserting_authority_is_dropped_whole(self) -> None:
        """One extra field is enough. The whole entry goes, with a note naming it.

        Dropping only the offending field would teach the next caller that those
        fields are decorative — the exact reading a prompt-injected answer is built
        to encourage.
        """
        registry = ActionRegistry.with_seeds()
        for field in AUTHORITY_FIELDS:
            with self.subTest(field=field):
                entry = {
                    "type": "message.send",
                    "parameters": {"to": "mom", "message": "hi"},
                    field: "whatever-the-model-said",
                }
                proposal = validate_proposal({"actions": [entry]}, registry.has)
                self.assertEqual(proposal.actions, ())
                self.assertTrue(
                    any(field in note for note in proposal.notes),
                    f"the note must name the field that was refused; got {proposal.notes!r}",
                )
                self.assertTrue(
                    any("asserted fields it does not own" in note for note in proposal.notes),
                    f"the note must say why the entry was dropped; got {proposal.notes!r}",
                )

    def test_a_plan_built_from_an_authority_asserting_answer_contains_no_such_field(self) -> None:
        """The *resulting* Plan — not just the validator's return value — is clean."""
        answer = {
            "actions": [
                {
                    "type": "message.send",
                    "parameters": {"to": "mom", "message": "hi"},
                    "risk_level": "low",
                    "permissions": ["*"],
                    "grant": "everything",
                    "approved": True,
                    "actor": "root",
                    "device_id": "someone-elses-laptop",
                    "policy": {"default_decision": "allow"},
                }
            ]
        }
        plan = _planner(answer).plan("tell mom I'll be late")
        text = _payload_text(plan)
        for field in AUTHORITY_FIELDS:
            self.assertNotIn(field, text, f"the plan payload leaked {field!r}: {text}")
        # The payload is exactly the shape the rest of the pipeline reads.
        self.assertEqual(
            set(plan.as_payload()),
            {"intent", "actions"},
            "a Plan may only carry an intent and proposed actions",
        )
        for action in plan.as_payload()["actions"]:
            self.assertEqual(set(action), {"type", "parameters"})
        self.assertTrue(
            any("asserted fields it does not own" in note for note in plan.notes),
            f"the drop must be visible in the notes; got {plan.notes!r}",
        )

    def test_an_unregistered_action_type_never_reaches_the_plan(self) -> None:
        plan = _planner(
            {"actions": [{"type": "nonesuch.teleport", "parameters": {"where": "moon"}}]}
        ).plan("teleport me to the moon")
        types = [action["type"] for action in plan.as_payload()["actions"]]
        self.assertNotIn("nonesuch.teleport", types)
        self.assertTrue(
            any("dropped unregistered action type 'nonesuch.teleport'" in note for note in plan.notes),
            f"the note must name the dropped type; got {plan.notes!r}",
        )

    def test_a_shell_shaped_action_type_is_not_in_the_plan(self) -> None:
        """If a shell capability is unregistered, naming it changes nothing."""
        registry = ActionRegistry.with_seeds()
        for action_type in SHELLISH:
            with self.subTest(action_type=action_type):
                self.assertFalse(
                    registry.has(action_type),
                    f"{action_type!r} is registered; this test's premise is wrong",
                )
                plan = _planner(
                    {"actions": [{"type": action_type, "parameters": {"command": "rm -rf /"}}]},
                    registry,
                ).plan(f"run {action_type}")
                types = [action["type"] for action in plan.as_payload()["actions"]]
                self.assertNotIn(action_type, types)
                self.assertTrue(
                    any(f"dropped unregistered action type {action_type!r}" in note for note in plan.notes),
                    f"the note must name {action_type!r}; got {plan.notes!r}",
                )

    # -- the fallback's honesty ---------------------------------------------

    def test_an_empty_or_malformed_answer_falls_back_and_says_so(self) -> None:
        cases = {
            "empty object": {},
            "no actions list": {"actions": "not-a-list"},
            "no actions at all": {"actions": []},
            "nothing": None,
        }
        for label, answer in cases.items():
            with self.subTest(case=label):
                plan = _planner(answer).plan("tell mom I'll be late")
                self.assertTrue(
                    any("deterministic planner answered instead" in note for note in plan.notes),
                    f"{label}: the fallback must be stated; got {plan.notes!r}",
                )
                self.assertFalse(
                    any(note.startswith("planned by") for note in plan.notes),
                    f"{label}: the plan must not claim the model planned it; got {plan.notes!r}",
                )

    def test_validate_proposal_refuses_a_non_mapping_answer(self) -> None:
        registry = ActionRegistry.with_seeds()
        for answer in ("a string", 17, ["a", "list"]):
            with self.subTest(answer=answer):
                with self.assertRaises(LlmError):
                    validate_proposal(answer, registry.has)

    def test_validate_proposal_refuses_an_answer_without_an_actions_list(self) -> None:
        registry = ActionRegistry.with_seeds()
        with self.assertRaises(LlmError):
            validate_proposal({"plan": []}, registry.has)

    # -- second door: the backend validates a *registered* action -----------

    def test_a_smuggled_parameter_is_rejected_by_the_registry(self) -> None:
        """The second door: the registry's schema check is the real validator.

        The model proposes a registered action — so the first door keeps it — with a
        nested ``{"command": "rm -rf /"}`` parameter that ``message.send`` does not
        accept. ``ActionRegistry.resolve`` is where an action's parameters are bound
        to its definition, so this is where the extra parameter must be refused.

        A warning is not a refusal: a warning is a sentence nobody acts on, and this
        action would be dispatched with the smuggled parameter intact.
        """
        registry = ActionRegistry.with_seeds()
        plan = _planner(
            {
                "actions": [
                    {
                        "type": "message.send",
                        "parameters": {
                            "to": "mom",
                            "message": "hi",
                            "command": {"command": "rm -rf /"},
                        },
                    }
                ]
            }
        ).plan("tell mom I'll be late")

        # The planner proposes it: the first door is about *which* fields an entry may
        # carry, not about the parameters of a registered action.
        self.assertEqual(
            [action["type"] for action in plan.as_payload()["actions"]],
            ["message.send"],
            "the proposal is expected to reach the registry; that is what makes this test",
        )
        smuggled = plan.as_payload()["actions"][0]["parameters"]
        self.assertIn("command", smuggled)

        # And the backend must refuse to bind it.
        raw = plan.as_payload()["actions"][0]
        with self.assertRaises(ValidationError):
            registry.resolve(raw)

    def test_the_authorization_path_rejects_the_smuggled_parameter(self) -> None:
        """The same refusal through the path the executor actually calls.

        ``authorize_graph`` resolves every node and turns a rejection into a DENY. A
        policy that allows the action and a grant that covers its risk are supplied so
        the *only* possible reason for refusal here is the parameter check — proving
        the assertion is about validation, not about the default-deny policy.
        """
        registry = ActionRegistry.with_seeds()
        graph = graph_from_dict(
            {
                "intent": "tell mom I'll be late",
                "nodes": [
                    {
                        "node_id": "s1",
                        "action": {
                            "type": "message.send",
                            "parameters": {
                                "to": "mom",
                                "message": "hi",
                                "command": {"command": "rm -rf /"},
                            },
                        },
                    },
                    {"node_id": "send", "node_type": "end", "depends_on": ["s1"]},
                ],
            }
        )
        context = build_context(
            None,
            None,
            Identity(id="operator"),
            grants=[PermissionGrant(scope="message.send", max_risk=RiskLevel.HIGH)],
            policy=Policy(
                rules=[PolicyRule(action="message.send", decision=PolicyDecision.ALLOW)],
                default_decision=PolicyDecision.ALLOW,
            ),
        )
        with self.assertRaises(GraphAuthorizationError):
            authorize_graph(graph, PolicyEngine(), registry, context)

    def test_the_authorization_path_does_reject_a_wrong_typed_parameter(self) -> None:
        """Control for the test above: the path *is* live, and it reports the reason.

        Without this, a rejection test that passes says nothing about whether the
        authorization path was exercised at all. A type mismatch is already refused,
        so this one is green by construction — and it is what makes the smuggled-key
        case above a real finding rather than a test that cannot fail.
        """
        registry = ActionRegistry.with_seeds()
        graph = graph_from_dict(
            {
                "intent": "tell mom I'll be late",
                "nodes": [
                    {
                        "node_id": "s1",
                        "action": {
                            "type": "message.send",
                            "parameters": {"to": 5, "message": "hi"},
                        },
                    },
                    {"node_id": "send", "node_type": "end", "depends_on": ["s1"]},
                ],
            }
        )
        context = build_context(
            None,
            None,
            Identity(id="operator"),
            grants=[PermissionGrant(scope="message.send", max_risk=RiskLevel.HIGH)],
            policy=Policy(
                rules=[PolicyRule(action="message.send", decision=PolicyDecision.ALLOW)],
                default_decision=PolicyDecision.ALLOW,
            ),
        )
        with self.assertRaises(GraphAuthorizationError) as caught:
            authorize_graph(graph, PolicyEngine(), registry, context)
        self.assertIn("registry rejected 'message.send'", str(caught.exception))
        self.assertIn("must be string", str(caught.exception))

    def test_validate_plan_alone_does_not_check_parameters(self) -> None:
        """Documents the seam: ``validate_plan`` is shape-only, by design.

        It validates the *plan* (intent, actions, dependency structure) and carries
        parameters through untouched. Deciding whether an action may accept them is
        the registry's job — so a test that only runs ``validate_plan`` proves
        nothing about the second door, which is exactly why the tests above go one
        layer deeper.
        """
        graph = validate_plan(
            {
                "intent": "tell mom I'll be late",
                "actions": [
                    {
                        "type": "message.send",
                        "parameters": {"to": "mom", "message": "hi", "command": "rm -rf /"},
                    }
                ],
            }
        )
        self.assertEqual(graph.nodes[0].action.parameters["command"], "rm -rf /")

if __name__ == "__main__":
    unittest.main()