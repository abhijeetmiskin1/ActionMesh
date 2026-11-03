"""C41 — the desktop provider through the real planner: raw in, policed downstream.

The bridge returns what the model said. These tests pin the two halves of that
sentence:

* :class:`DesktopProvider` hands back the mapping **exactly** as the bridge produced it
  — no filtering, no added fields, no pre-validation — because a provider that tidied
  the answer would make :func:`validate_proposal` police different bytes depending on
  which provider was configured;
* :class:`LlmPlanner` still refuses to execute anything it does not recognise, and its
  fallback note says whether the model was silent or merely unhelpful. Those are
  different problems for a user, so they are different sentences.
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest import mock

from actionmesh.core.planner import ProposedAction, RuleBasedPlanner
from actionmesh.voiceos.llm import (
    ENV_BRIDGE_SOCKET,
    ENV_BRIDGE_TOKEN,
    AnalysisRequest,
    DesktopAnalyst,
    DesktopProvider,
    LlmConfig,
    LlmError,
    LlmPlanner,
    bridge_config_from_env,
    build_analyst,
    build_provider,
)
from actionmesh.voiceos.model_bridge import BridgeConfig, BridgeError

BRIDGE = BridgeConfig(socket_path="/tmp/provider-bridge.sock", token="token-not-a-key")

CATALOG = (("process_list", "List the processes on this computer"),)


def _desktop(**kwargs: Any) -> DesktopProvider:
    options: dict[str, Any] = {
        "name": "desktop",
        "bridge": BRIDGE,
        "model": "claude-sonnet-5-5",
        "catalog": lambda: CATALOG,
    }
    options.update(kwargs)
    return DesktopProvider(**options)


def _planner(provider: Any, known: Any = None) -> LlmPlanner:
    return LlmPlanner(
        provider=provider,
        fallback=RuleBasedPlanner(),
        known_action=known or (lambda name: name == "process_list"),
    )


class TestDesktopProviderPassesTheAnswerThrough(unittest.TestCase):
    def test_propose_returns_the_exact_raw_mapping(self) -> None:
        raw = {
            "actions": [
                {"type": "process_list", "parameters": {"limit": 5}, "risk_level": "low"},
                {"type": "system.wipe", "parameters": {}},
            ],
            "notes": ["said something"],
        }
        with mock.patch(
            "actionmesh.voiceos.llm.model_bridge.generate", return_value=raw
        ):
            answer = _desktop().propose("what is running?")
        self.assertIs(answer, raw, "the provider must not copy, filter or reshape")
        self.assertIn("risk_level", answer["actions"][0])

    def test_the_call_is_plan_shaped_and_carries_the_catalogue(self) -> None:
        with mock.patch(
            "actionmesh.voiceos.llm.model_bridge.generate", return_value={"actions": []}
        ) as generate:
            _desktop().propose("what is running?")
        args, kwargs = generate.call_args
        self.assertIs(args[0], BRIDGE)
        self.assertEqual(kwargs["purpose"], "plan")
        self.assertIn("what is running?", kwargs["user"])
        self.assertIn("process_list", kwargs["user"])
        self.assertIn("List the processes on this computer", kwargs["user"])
        self.assertTrue(kwargs["system"].strip())

    def test_a_bridge_failure_becomes_an_llm_error_naming_the_state(self) -> None:
        with mock.patch(
            "actionmesh.voiceos.llm.model_bridge.generate",
            side_effect=BridgeError("missing_key", "No API key is stored."),
        ), self.assertRaises(LlmError) as raised:
            _desktop().propose("hi")
        self.assertIn("missing_key", str(raised.exception))
        self.assertIn("No API key is stored.", str(raised.exception))

    def test_the_analyst_sends_the_diagnosis_shape(self) -> None:
        request = AnalysisRequest(
            question="why did it stop?",
            excerpt="line one\nline two",
            facts="it exited",
            computer="laptop",
            source="app.log",
            truncated=True,
        )
        with mock.patch(
            "actionmesh.voiceos.llm.model_bridge.generate", return_value={"finding": "x"}
        ) as generate:
            answer = DesktopAnalyst(name="desktop", bridge=BRIDGE).explain(request)
        self.assertEqual(answer, {"finding": "x"})
        _, kwargs = generate.call_args
        self.assertEqual(kwargs["purpose"], "diagnose")
        for fragment in ("why did it stop?", "line one", "it exited", "laptop", "app.log"):
            self.assertIn(fragment, kwargs["user"])
        self.assertIn("tail of a larger log", kwargs["user"])


class TestThePlannerStillPoliciesTheAnswer(unittest.TestCase):
    def test_a_risk_level_is_dropped_with_a_note(self) -> None:
        raw = {
            "actions": [
                {
                    "type": "process_list",
                    "parameters": {},
                    "risk_level": "low",
                    "approved": True,
                }
            ]
        }
        with mock.patch("actionmesh.voiceos.llm.model_bridge.generate", return_value=raw):
            plan = _planner(_desktop()).plan("what is running?")
        # The hostile entry is not in the model's part of the plan. The deterministic
        # fallback still answers — that is the planner's contract — so the assertion
        # is about what the *model* contributed and what the note says, not about the
        # final plan being empty.
        notes = " ".join(plan.notes)
        self.assertIn("asserted fields it does not own", notes)
        self.assertIn("deterministic planner answered instead", notes)
        self.assertNotIn("planned by desktop", notes)
        # The fallback's action is the rule-based planner's own, not the model's
        # entry with its authority fields quietly stripped.
        self.assertEqual([a.type for a in plan.actions], ["device.process.list"])

    def test_a_registered_action_becomes_a_plan(self) -> None:
        raw = {"actions": [{"type": "process_list", "parameters": {}}]}
        with mock.patch("actionmesh.voiceos.llm.model_bridge.generate", return_value=raw):
            plan = _planner(_desktop()).plan("what is running?")
        self.assertEqual(
            plan.actions, (ProposedAction(type="process_list", parameters={}),)
        )
        self.assertIn("planned by desktop", " ".join(plan.notes))

    def test_an_unregistered_action_never_reaches_the_plan(self) -> None:
        raw = {"actions": [{"type": "system.wipe_everything", "parameters": {}}]}
        with mock.patch("actionmesh.voiceos.llm.model_bridge.generate", return_value=raw):
            plan = _planner(_desktop()).plan("wipe everything")
        self.assertNotIn("system.wipe_everything", [a.type for a in plan.actions])
        self.assertIn("unregistered action type", " ".join(plan.notes))

    def test_an_empty_actions_list_falls_back_saying_nothing_usable(self) -> None:
        with mock.patch(
            "actionmesh.voiceos.llm.model_bridge.generate", return_value={"actions": []}
        ):
            plan = _planner(_desktop()).plan("what is running?")
        notes = " ".join(plan.notes)
        self.assertIn("the model proposed nothing usable", notes)
        self.assertNotIn("did not answer", notes)

    def test_a_bridge_error_falls_back_saying_the_model_did_not_answer(self) -> None:
        with mock.patch(
            "actionmesh.voiceos.llm.model_bridge.generate",
            side_effect=BridgeError("missing_key", "No API key is stored."),
        ):
            plan = _planner(_desktop()).plan("what is running?")
        notes = " ".join(plan.notes)
        self.assertIn("the model did not answer this request", notes)
        self.assertIn("missing_key", notes)
        self.assertNotIn("proposed nothing usable", notes)
        # The deterministic planner's own answer still arrives.
        self.assertEqual([a.type for a in plan.actions], ["device.process.list"])
        self.assertNotIn("planned by desktop", notes)

    def test_the_two_fallback_notes_are_distinguishable(self) -> None:
        """A user must be able to tell "no answer" from "an answer with nothing in it"."""
        with mock.patch(
            "actionmesh.voiceos.llm.model_bridge.generate", return_value={"actions": []}
        ):
            silent_plan = _planner(_desktop()).plan("what is running?")
        with mock.patch(
            "actionmesh.voiceos.llm.model_bridge.generate",
            side_effect=BridgeError("timeout", ""),
        ):
            failed_plan = _planner(_desktop()).plan("what is running?")
        self.assertNotEqual(silent_plan.notes[0], failed_plan.notes[0])


class TestBuildProviderResolvesDesktop(unittest.TestCase):
    def test_no_bridge_names_the_variables_an_operator_must_set(self) -> None:
        provider, reason = build_provider(LlmConfig(provider="desktop"))
        self.assertIsNone(provider)
        self.assertIn(ENV_BRIDGE_SOCKET, reason)
        self.assertIn(ENV_BRIDGE_TOKEN, reason)
        self.assertIn("deterministic planner", reason)

    def test_an_incomplete_bridge_is_the_same_as_none(self) -> None:
        provider, reason = build_provider(
            LlmConfig(provider="desktop"),
            bridge=BridgeConfig(socket_path="/tmp/x.sock", token=""),
        )
        self.assertIsNone(provider)
        self.assertIn(ENV_BRIDGE_TOKEN, reason)

    def test_a_configured_bridge_yields_a_desktop_provider(self) -> None:
        provider, reason = build_provider(
            LlmConfig(provider="desktop"), bridge=BRIDGE, catalog=lambda: CATALOG
        )
        self.assertEqual(reason, "")
        assert isinstance(provider, DesktopProvider)
        self.assertEqual(provider.name, "desktop")
        self.assertEqual(provider.bridge, BRIDGE)
        self.assertEqual(provider.catalog(), CATALOG)

    def test_the_bridge_config_is_read_from_the_environment(self) -> None:
        config = bridge_config_from_env(
            {ENV_BRIDGE_SOCKET: " /tmp/s.sock ", ENV_BRIDGE_TOKEN: " tok "}
        )
        self.assertEqual(config.socket_path, "/tmp/s.sock")
        self.assertEqual(config.token, "tok")
        self.assertTrue(config.configured)
        self.assertFalse(bridge_config_from_env({}).configured)

    def test_build_analyst_resolves_desktop_the_same_way(self) -> None:
        provider, reason = build_analyst(LlmConfig(provider="desktop"))
        self.assertIsNone(provider)
        self.assertIn(ENV_BRIDGE_SOCKET, reason)
        provider, reason = build_analyst(LlmConfig(provider="desktop"), bridge=BRIDGE)
        self.assertEqual(reason, "")
        assert isinstance(provider, DesktopAnalyst)

    def test_the_token_never_appears_in_a_reason(self) -> None:
        _, reason = build_provider(
            LlmConfig(provider="desktop"),
            bridge=BridgeConfig(socket_path="/tmp/x.sock", token="tok-secret-value"),
        )
        self.assertNotIn("tok-secret-value", reason)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()