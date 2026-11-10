"""C7 domain-specific mock provider tests."""

from __future__ import annotations

import asyncio
import unittest

from actionmesh.protocol.models import Action, FailureKind
from actionmesh.providers import (
    MockCalendarProvider,
    MockContactsProvider,
    MockFileProvider,
    MockMessagingProvider,
    MockNotificationProvider,
    MockPaymentProvider,
    MockProvider,
    ProviderError,
    default_mock_providers,
)


def run(coro):
    return asyncio.run(coro)


class TestDomainProviders(unittest.TestCase):
    def test_default_routing_keeps_generic_fallback_last(self) -> None:
        providers = default_mock_providers()
        self.assertIsInstance(providers[0], MockMessagingProvider)
        self.assertIsInstance(providers[-1], MockProvider)
        action = Action(type="message.send", parameters={"message": "hello"})
        matches = [provider for provider in providers if provider.supports(action)]
        self.assertIsInstance(matches[0], MockMessagingProvider)

    def test_messaging_records_message_and_observation(self) -> None:
        provider = MockMessagingProvider()
        action = Action(type="message.send", parameters={"to": "mom", "message": "hello"})
        result = run(provider.execute(action))
        self.assertEqual(len(provider.thread), 1)
        self.assertEqual(provider.thread[0]["message"], "hello")
        observed = provider.observe(action)
        self.assertEqual(observed["action_type"], "message.send")
        self.assertEqual(observed["idempotency_key"], action.idempotency_key)
        self.assertEqual(observed["parameters"], action.parameters)
        self.assertTrue(result.simulated)

    def test_messaging_unknown_recipient_is_invalid_input(self) -> None:
        provider = MockMessagingProvider()
        with self.assertRaises(ProviderError) as raised:
            run(provider.execute(Action(
                type="message.send",
                parameters={"to": "unknown", "message": "hello"},
            )))
        self.assertIs(raised.exception.kind, FailureKind.INVALID_INPUT)

    def test_calendar_records_event(self) -> None:
        provider = MockCalendarProvider()
        action = Action(type="calendar.create", parameters={"title": "Standup"})
        run(provider.execute(action))
        self.assertEqual(next(iter(provider.events.values()))["title"], "Standup")
        self.assertIsNotNone(provider.observe(action))

    def test_files_records_move_and_observation(self) -> None:
        provider = MockFileProvider(initial={"/a.txt": "data"})
        action = Action(
            type="file.move", parameters={"path": "/a.txt", "destination": "/b.txt"}
        )
        run(provider.execute(action))
        self.assertEqual(provider.files, {"/b.txt": "data"})
        self.assertEqual(provider.observe(action)["action_type"], "file.move")

    def test_files_private_path_is_authorization_failure(self) -> None:
        provider = MockFileProvider(initial={"/private/secret.txt": "secret"})
        with self.assertRaises(ProviderError) as raised:
            run(provider.execute(Action(type="file.read", parameters={"path": "/private/secret.txt"})))
        self.assertIs(raised.exception.kind, FailureKind.AUTHORIZATION)

    def test_notifications_record_reminder(self) -> None:
        provider = MockNotificationProvider()
        action = Action(type="reminder.create", parameters={"text": "Call"})
        run(provider.execute(action))
        self.assertEqual(next(iter(provider.reminders.values()))["status"], "scheduled")
        self.assertIsNotNone(provider.observe(action))

    def test_contacts_resolve_without_executable_side_effect(self) -> None:
        provider = MockContactsProvider()
        self.assertEqual(provider.resolve(" MOM ")["name"], "Mom")
        self.assertIsNone(provider.resolve("nobody"))
        self.assertFalse(provider.supports(Action(type="contact.resolve")))
        self.assertIsNone(provider.observe(Action(type="contact.resolve")))

    def test_payment_transitions_to_settled(self) -> None:
        provider = MockPaymentProvider()
        prepare = Action(
            type="payment.prepare", parameters={"amount": 10, "recipient": "shop"}
        )
        result = run(provider.execute(prepare))
        payment_id = result.data["payment_id"]
        self.assertEqual(provider.intents[payment_id]["state"], "prepared")
        run(provider.execute(Action(type="payment.confirm", parameters={"payment_id": payment_id})))
        self.assertEqual(provider.intents[payment_id]["state"], "confirmed")
        self.assertEqual(provider.settle(payment_id)["state"], "settled")
        self.assertEqual(provider.observe(prepare)["action_type"], "payment.prepare")

    def test_payment_can_fail_after_confirmation(self) -> None:
        provider = MockPaymentProvider()
        prepare = Action(
            type="payment.prepare", parameters={"amount": 10, "recipient": "shop"}
        )
        payment_id = run(provider.execute(prepare)).data["payment_id"]
        run(provider.execute(Action(type="payment.confirm", parameters={"payment_id": payment_id})))
        self.assertEqual(provider.fail(payment_id)["state"], "failed")

    def test_payment_failure_kinds_are_domain_specific(self) -> None:
        provider = MockPaymentProvider()
        for recipient, kind in (("declined", FailureKind.AUTHORIZATION),
                                ("gateway_down", FailureKind.PROVIDER)):
            with self.subTest(recipient=recipient):
                with self.assertRaises(ProviderError) as raised:
                    run(provider.execute(Action(
                        type="payment.prepare",
                        parameters={"amount": 10, "recipient": recipient},
                    )))
                self.assertIs(raised.exception.kind, kind)


if __name__ == "__main__":
    unittest.main()
