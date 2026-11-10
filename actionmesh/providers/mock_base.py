"""Shared harness for deterministic mock providers (C7).

Every mock provider gets the same boring machinery — call tracking, attempt
counting, deterministic failure injection, idempotent receipts, observable
state — so domain modules only implement what is actually domain-specific:
their action types and their ``_handle()``.

Design notes:

* No network, no clock dependence, no randomness. The same inputs always
  produce the same outputs, which is what makes these usable in tests.
* ``execute()`` consults ``failures``/``flaky`` *before* touching domain
  state, so an injected failure never half-applies.
* Receipts are keyed by ``idempotency_key``: repeating a request returns the
  stored record instead of performing the effect twice.
* ``observe()`` returns the stored record (action type, idempotency key,
  receipt id, parameters) — the fields C6's verification engine needs. A
  provider that claims success without recording anything is caught by C6.

This module deliberately does not import ``actionmesh.providers.base`` at
runtime (only under ``TYPE_CHECKING``): ``base.py`` imports this module, so a
runtime import here would be circular.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from actionmesh.protocol.models import Action, VerificationResult
    from actionmesh.providers.base import ProviderError, ProviderResult


class MockHarnessMixin:
    """Deterministic in-memory machinery shared by every mock provider.

    Concrete providers declare ``name`` and ``SUPPORTED`` (action types),
    implement ``supports()``, and implement ``_handle()`` for the
    domain-specific part of execution.
    """

    name: str = "mock-base"
    SUPPORTED: frozenset[str] = frozenset()

    def __init__(self) -> None:
        # Receipts keyed by idempotency key — the idempotency mechanism.
        self.state: dict[str, Any] = {}
        # Every dispatch attempt, including injected failures.
        self.calls: list[Action] = []
        # Map an action type to a ProviderError to raise instead of succeeding.
        self.failures: dict[str, ProviderError] = {}
        # Map an action type to "succeed but fail verification".
        self.verify_failures: set[str] = set()
        # Types that fail until attempt N (1-indexed), to exercise retry.
        self.flaky: dict[str, int] = {}
        self._attempts: dict[str, int] = {}

    async def execute(self, action: Action) -> ProviderResult:
        """Dispatch with failure injection, then idempotent recording."""
        from actionmesh.protocol.models import FailureKind
        from actionmesh.providers.base import ProviderError, ProviderResult

        self.calls.append(action)
        self._attempts[action.type] = self._attempts.get(action.type, 0) + 1

        injected = self.failures.get(action.type)
        if injected is not None:
            raise injected

        fail_until = self.flaky.get(action.type)
        if fail_until is not None and self._attempts[action.type] < fail_until:
            raise ProviderError(
                f"transient failure on attempt {self._attempts[action.type]}",
                FailureKind.TRANSIENT,
            )

        record = self.state.get(action.idempotency_key)
        if record is None:
            extras = await self._handle(action)
            record = {
                "receipt_id": f"rcpt_{action.idempotency_key}",
                "action_type": action.type,
                "parameters": dict(action.parameters),
            }
            record.update(extras)
            self.state[action.idempotency_key] = record

        return ProviderResult(
            ok=True,
            provider=self.name,
            action_id=action.action_id,
            data=dict(record),
            simulated=self.simulated,
        )

    async def _handle(self, action: Action) -> dict[str, Any]:
        """Domain-specific execution. Returns extra receipt fields.

        May raise ``ProviderError`` for domain failures (unknown entity,
        invalid domain state, ...). The harness guarantees an injected
        failure never reaches this point.
        """
        return {}

    #: All harness-based providers are simulators. A plain class attribute
    #: rather than a property, so it stays writable and can sit alongside the
    #: equally writable ``ActionProvider.simulated`` without a Liskov clash.
    simulated: bool = True

    def observe(self, action: Action) -> dict[str, Any] | None:
        """Expose the record this action actually produced.

        C6's verification engine reads this instead of trusting ``execute``.
        Returning the stored record — rather than a fresh success — means a
        provider that claims success without doing anything is caught.
        """
        record = self.state.get(action.idempotency_key)
        if record is None:
            return None
        return {
            "action_type": action.type,
            "idempotency_key": action.idempotency_key,
            "receipt_id": record.get("receipt_id"),
            "parameters": record.get("parameters"),
        }

    async def verify(
        self, action: Action, result: ProviderResult
    ) -> VerificationResult:
        from actionmesh.protocol.models import VerificationResult

        if action.type in self.verify_failures:
            return VerificationResult(
                verified=False,
                method="mock_state_check",
                detail=f"mock verification forced to fail for {action.type}",
                simulated=True,
                evidence={"action_type": action.type},
            )
        present = action.idempotency_key in self.state
        return VerificationResult(
            verified=present,
            method="mock_state_check",
            detail="receipt present in mock state" if present else "no receipt recorded",
            simulated=True,
            evidence={
                "action_type": action.type,
                "idempotency_key": action.idempotency_key,
            },
        )
