"""Conversation remains advisory and cannot bypass proposal validation."""
from dataclasses import dataclass, field
from typing import Any

import pytest

from actionmesh.agent.definitions import register_device_actions
from actionmesh.core.planner import RuleBasedPlanner
from actionmesh.core.registry import ActionRegistry
from actionmesh.voiceos.llm import (
    LlmError,
    LlmPlanner,
    _plan_user_text,
    build_catalogue,
    validate_proposal,
)
from actionmesh.voiceos.models import Conversation
from actionmesh.voiceos.reasoning import Reasoner


@dataclass
class Provider:
    answer: dict[str, Any]
    name: str = "fixture-model"
    seen: list[str] = field(default_factory=list)

    def propose(self, request: str) -> dict[str, Any]:
        self.seen.append(request)
        return self.answer


def reasoner(provider: Provider) -> Reasoner:
    return Reasoner(
        planner=LlmPlanner(provider, RuleBasedPlanner(), lambda _: False),
        known_action=lambda _: False,
        templates=lambda: (),
    )


@pytest.mark.parametrize("utterance", ["Hello", "How do I delete a file?", "Delete it", "फाइल क्या है?"])
def test_model_answers_and_clarifications_do_not_create_action_tasks(utterance: str) -> None:
    provider = Provider({"actions": [], "reply": "Which file do you mean?"})
    result = reasoner(provider).understand(utterance, Conversation(actor_id="speaker"))
    assert result.kind == "answer"
    assert result.session is None
    assert "Which file" in result.reply
    assert "No device action has run." in result.reply
    assert result.notes == ("answered by fixture-model",)


@pytest.mark.parametrize("reply", [None, 123, {}, "x" * 4001])
def test_malformed_conversational_replies_are_refused(reply: Any) -> None:
    with pytest.raises(LlmError):
        validate_proposal({"actions": [], "reply": reply}, lambda _: True)


def test_discarded_or_action_bearing_proposals_never_supply_conversational_success() -> None:
    for action in [
        {"type": "unknown", "parameters": {}},
        {"type": "known", "parameters": {}, "approved": True},
        {"type": "known", "parameters": {}},
    ]:
        proposal = validate_proposal({"actions": [action], "reply": "Done"}, lambda t: t == "known")
        assert proposal.reply == ""
    assert validate_proposal({"actions": [], "reply": "Done", "grants": []}, lambda _: True).reply == ""


def test_model_context_is_bounded_and_never_contains_observations_or_assistant_diagnostics() -> None:
    conversation = Conversation(actor_id="speaker")
    for i in range(5):
        conversation.remember("user", f"request-{i}")
    conversation.remember("voiceos", "PRIVATE diagnostic log or file observation")
    provider = Provider({"actions": [], "reply": "A helpful explanation"})
    result = reasoner(provider).understand("Explain that", conversation)
    assert result.session is None
    sent = provider.seen[0]
    assert "PRIVATE" not in sent
    assert "request-0" not in sent and "request-1" not in sent
    assert all(f"request-{i}" in sent for i in (2, 3, 4))
    assert sent.endswith("Current user request: Explain that")
    assert len(reasoner(Provider({"actions": []})).planner.plan("unknown").actions) == 0


def test_full_current_capability_catalogue_fits_without_silent_omissions() -> None:
    registry = ActionRegistry.with_seeds()
    register_device_actions(registry)
    catalogue = build_catalogue(registry.list())()
    text = _plan_user_text(catalogue, "What can you do?")
    assert "actions omitted" not in text
    assert all(f"- {name} —" in text for name, _ in catalogue)


def test_catalogue_describes_required_inputs_without_sending_defaults_or_examples() -> None:
    from dataclasses import replace

    from actionmesh.core.registry import seed_definitions

    action = replace(seed_definitions()[0], name="fixture.action", input_schema={
        "type": "object", "required": ["path"], "properties": {
            "path": {"type": "string", "default": "PRIVATE_DEFAULT", "examples": ["PRIVATE_SAMPLE"]},
            "limit": {"type": "integer"},
        },
    })
    catalogue = build_catalogue([action])
    text = str(catalogue())
    assert "path:string*" in text
    assert "limit:integer" in text
    assert "PRIVATE_" not in text
    initial = catalogue()
    initial.clear()  # Callers cannot mutate the cached catalogue.
    assert catalogue()


@pytest.mark.parametrize("utterance", ["टेक क्रिंशोट", "स्क्रीनशॉट ले लो।", "Take a screenshot!", "I said, please take a screenshot!"])
def test_screenshot_commands_plan_without_using_a_model(utterance: str) -> None:
    provider = Provider({"actions": [], "reply": "unexpected"})
    assistant = reasoner(provider)
    assistant.known_action = lambda name: name == "device.screen.capture"
    result = assistant.understand(utterance, Conversation(actor_id="speaker"))
    assert result.kind == "task"
    assert result.session is not None
    step, = result.session.steps
    assert step.action_type == "device.screen.capture"
    assert step.parameters == {}
    assert step.state == "pending"
    assert "Run" in result.reply
    assert provider.seen == []


def test_screenshot_command_without_capture_capability_is_unavailable() -> None:
    provider = Provider({"actions": [], "reply": "unexpected"})
    result = reasoner(provider).understand("टेक क्रिंशोट", Conversation(actor_id="speaker"))
    assert result.session is None
    assert "unavailable" in result.reply
    assert provider.seen == []


def test_show_my_files_does_not_match_a_food_discovery_workflow() -> None:
    provider = Provider({"actions": [], "reply": "unexpected"})
    assistant = reasoner(provider)
    assistant.known_action = lambda name: name == "device.file.list"
    result = assistant.understand("Show me my files.", Conversation(actor_id="speaker"))
    assert result.session is not None
    assert [step.action_type for step in result.session.steps] == ["device.file.list"]
    assert provider.seen == []


@pytest.mark.parametrize(("phrase", "app"), [
    ("Open browser", "browser"),
    ("Open Calculator!", "calculator"),
    ("Launch Firefox", "firefox"),
])
def test_common_app_names_plan_real_launches(phrase: str, app: str) -> None:
    provider = Provider({"actions": [], "reply": "unexpected"})
    assistant = reasoner(provider)
    assistant.known_action = lambda name: name == "device.app.open"
    result = assistant.understand(phrase, Conversation(actor_id="speaker"))
    assert result.session is not None
    step, = result.session.steps
    assert step.action_type == "device.app.open"
    assert step.parameters == {"app": app}
    assert provider.seen == []


def test_unclear_app_name_can_be_clarified_by_the_model() -> None:
    provider = Provider({"actions": [], "reply": "Which app should I open?"})
    assistant = reasoner(provider)
    assistant.known_action = lambda name: name == "device.app.open"
    result = assistant.understand("Open Börgün", Conversation(actor_id="speaker"))
    assert result.session is None
    assert "Which app" in result.reply
    assert provider.seen == ["Open Börgün"]


@pytest.mark.parametrize(("phrase", "action"), [
    ("Play music", "media.play"),
    ("Pause music", "media.pause"),
    ("गाना चलाओ।", "media.play"),
    ("गाना रोक दो", "media.pause"),
])
def test_common_media_requests_skip_model_planning(phrase: str, action: str) -> None:
    provider = Provider({"actions": [], "reply": "unexpected"})
    assistant = reasoner(provider)
    assistant.known_action = lambda name: name == action
    result = assistant.understand(phrase, Conversation(actor_id="speaker"))
    assert result.session is not None
    step, = result.session.steps
    assert step.action_type == action
    assert not step.parameters
    assert provider.seen == []


def test_named_file_search_is_confined_and_preserves_literal_glob_characters() -> None:
    from fnmatch import fnmatchcase

    provider = Provider({"actions": [], "reply": "unexpected"})
    assistant = reasoner(provider)
    assistant.known_action = lambda name: name == "device.files.search"
    result = assistant.understand(
        'Find files named "Report[1]*" in folder Documents', Conversation(actor_id="speaker"),
    )
    assert result.session is not None
    step, = result.session.steps
    assert step.action_type == "device.files.search"
    assert step.parameters["path"] == "Documents"
    assert fnmatchcase("Report[1]*.txt", step.parameters["pattern"])
    assert not fnmatchcase("Report1.txt", step.parameters["pattern"])
    assert provider.seen == []


def test_largest_files_request_uses_the_existing_confined_search() -> None:
    provider = Provider({"actions": [], "reply": "unexpected"})
    assistant = reasoner(provider)
    assistant.known_action = lambda name: name == "device.files.search"
    result = assistant.understand(
        "I want list highest storage files in folder Documents", Conversation(actor_id="speaker"),
    )
    assert result.session is not None
    step, = result.session.steps
    assert step.action_type == "device.files.search"
    assert step.parameters == {"path": "Documents", "pattern": "*", "sort_by": "size", "limit": 10}
    assert provider.seen == []


@pytest.mark.parametrize("phrase", [
    "Give me footlink details of my storage.",
    "How much space do I have left?",
    "Show my disk space.",
    "How much storage is free?",
])
def test_storage_questions_route_without_the_model(phrase: str) -> None:
    provider = Provider({"actions": [], "reply": "unexpected"})
    assistant = reasoner(provider)
    assistant.known_action = lambda name: name == "device.files.storage"
    result = assistant.understand(phrase, Conversation(actor_id="speaker"))
    assert result.session is not None
    step, = result.session.steps
    assert step.action_type == "device.files.storage"
    assert step.parameters == {}
    assert not provider.seen


@pytest.mark.parametrize("phrase", ["What is storage in Python?", "Check my cloud storage."])
def test_storage_words_alone_do_not_trigger_a_device_read(phrase: str) -> None:
    provider = Provider({"actions": [], "reply": "This is a general question."})
    assistant = reasoner(provider)
    assistant.known_action = lambda name: name == "device.files.storage"
    result = assistant.understand(phrase, Conversation(actor_id="speaker"))
    assert result.session is None
    assert provider.seen == [phrase]


@pytest.mark.parametrize(("phrase", "action", "parameters"), [
    ("Stop the music", "media.pause", {}),
    ("Open gnome-calculator", "device.app.open", {"app": "gnome-calculator"}),
    ("ब्राउज़र खोल दो", "device.app.open", {"app": "browser"}),
    ("Status of my laptop drive", "device.files.storage", {}),
])
def test_natural_device_requests_reach_the_registered_action_planner(
    phrase: str, action: str, parameters: dict[str, Any],
) -> None:
    registry = ActionRegistry()
    register_device_actions(registry)
    provider = Provider({"actions": [{"type": action, "parameters": parameters}]})
    assistant = Reasoner(
        planner=LlmPlanner(provider, RuleBasedPlanner(), registry.has, registry=registry),
        known_action=registry.has,
        templates=lambda: (),
    )
    result = assistant.understand(phrase, Conversation(actor_id="speaker"))
    assert provider.seen == [phrase]
    assert result.session is not None
    step, = result.session.steps
    assert (step.action_type, step.parameters) == (action, parameters)
    assert step.state == "pending"


@pytest.mark.parametrize("bad_step", [
    {"type": "device.app.open", "parameters": {}},
    {"type": "device.app.open", "parameters": {"app": 123}},
    {"type": "not.registered", "parameters": {}},
])
def test_model_does_not_stage_half_a_request(
    bad_step: dict[str, Any],
) -> None:
    registry = ActionRegistry()
    register_device_actions(registry)
    provider = Provider({"actions": [{"type": "media.pause", "parameters": {}}, bad_step]})
    planner = LlmPlanner(provider, RuleBasedPlanner(), registry.has, registry=registry)
    plan = planner.plan("Stop playback, then open my app")
    assert not plan.actions
    assert plan.reply
