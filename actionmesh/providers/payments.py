"""C7 — Payments mock provider.

The domain where honesty matters most. This provider models an explicit payment
state machine, refuses to skip states, and refuses to move money it does not
hold — all without moving any real money. `simulated = True`, always.

States::

    prepared → authorized → confirmed → settled
                       ↘ cancelled / failed

* ``payment.prepare`` creates a ``prepared`` intent. Amounts must be positive
  numbers, recipients non-empty strings. A recipient named ``"declined"`` is
  deterministically refused with AUTHORIZATION (a declined instrument), so
  tests can exercise the failure path without randomness.
* ``payment.confirm`` moves ``prepared → confirmed``. Confirming anything but a
  prepared intent is INVALID_INPUT. Confirming the same intent twice raises
  INVALID_INPUT rather than double-confirming.
* ``settle()`` and ``fail()`` complete a confirmed intent. They are explicit
    provider lifecycle operations, not registry actions.
* ``payment.verify`` reads the recorded state — never changes it. Verifying an
  unknown ``payment_id`` is REFUTED input (INVALID_INPUT).

Settlement itself is out of scope: this is a mock, and inventing a settlement
rail would imply a completeness the provider does not have.
"""

from __future__ import annotations

from typing import Any

from actionmesh.protocol.models import Action, FailureKind
from actionmesh.providers.base import ActionProvider, ProviderError
from actionmesh.providers.mock_base import MockHarnessMixin

_DECLINED = "declined"


class MockPaymentProvider(MockHarnessMixin, ActionProvider):
    """MOCK payments: an explicit intent state machine. No real money moves."""

    name = "mock-payments"
    SUPPORTED = frozenset({"payment.prepare", "payment.confirm", "payment.verify"})

    def supports(self, action: Action) -> bool:
        return action.type in self.SUPPORTED and action.provider in ("mock", self.name)

    def __init__(self) -> None:
        super().__init__()
        # payment_id -> intent record (state, amount, recipient, method).
        self.intents: dict[str, dict[str, Any]] = {}

    async def _handle(self, action: Action) -> dict[str, Any]:
        params = action.parameters
        if action.type == "payment.prepare":
            amount = params.get("amount")
            recipient = params.get("recipient")
            if (
                not isinstance(amount, (int, float))
                or isinstance(amount, bool)
                or amount <= 0
            ):
                raise ProviderError("amount must be a positive number",
                                    FailureKind.INVALID_INPUT)
            if not isinstance(recipient, str) or not recipient.strip():
                raise ProviderError("recipient must be a non-empty string",
                                    FailureKind.INVALID_INPUT)
            if recipient.strip().lower() == _DECLINED:
                # A declined instrument: authorization failure, not transient.
                # Retrying would just decline again.
                raise ProviderError("instrument declined", FailureKind.AUTHORIZATION)
            if recipient.strip().lower() == "gateway_down":
                raise ProviderError("payment gateway unavailable", FailureKind.PROVIDER)
            payment_id = f"pay_{len(self.intents) + 1:04d}"
            self.intents[payment_id] = {
                "payment_id": payment_id,
                "amount": amount,
                "recipient": recipient,
                "method": params.get("method", "mock"),
                "state": "prepared",
            }
            return {"payment_id": payment_id, "state": "prepared"}

        if action.type == "payment.confirm":
            raw_payment_id = params.get("payment_id")
            intent = self.intents.get(str(raw_payment_id))
            if intent is None:
                raise ProviderError(f"unknown payment {raw_payment_id!r}",
                                    FailureKind.INVALID_INPUT)
            if intent["state"] != "prepared":
                # Confirming twice must not double-confirm.
                raise ProviderError(
                    f"payment {raw_payment_id!r} is {intent['state']}, not prepared",
                    FailureKind.INVALID_INPUT,
                )
            intent["state"] = "confirmed"
            return {"payment_id": raw_payment_id, "state": "confirmed"}

        if action.type == "payment.verify":
            raw_payment_id = params.get("payment_id")
            intent = self.intents.get(str(raw_payment_id))
            if intent is None:
                raise ProviderError(f"unknown payment {raw_payment_id!r}",
                                    FailureKind.INVALID_INPUT)
            return {"payment_id": raw_payment_id, "state": intent["state"]}

        raise ProviderError(f"unsupported action {action.type!r}", FailureKind.PERMANENT)

    def settle(self, payment_id: str) -> dict[str, Any]:
        """Move a confirmed intent to its terminal settled state."""
        intent = self._transition(payment_id, "confirmed", "settled")
        return {"payment_id": payment_id, "state": intent["state"]}

    def fail(self, payment_id: str) -> dict[str, Any]:
        """Move a confirmed intent to its terminal failed state."""
        intent = self._transition(payment_id, "confirmed", "failed")
        return {"payment_id": payment_id, "state": intent["state"]}

    def _transition(self, payment_id: str, expected: str, target: str) -> dict[str, Any]:
        intent = self.intents.get(str(payment_id))
        if intent is None:
            raise ProviderError(f"unknown payment {payment_id!r}", FailureKind.INVALID_INPUT)
        if intent["state"] != expected:
            raise ProviderError(
                f"payment {payment_id!r} is {intent['state']}, not {expected}",
                FailureKind.INVALID_INPUT,
            )
        intent["state"] = target
        return intent


__all__ = ["MockPaymentProvider"]
