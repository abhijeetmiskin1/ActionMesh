"""Provider adapters.

A provider is the outermost boundary: the only place a real-world side effect
can occur. The core engine never knows which vendor it is talking to — it only
knows the ``ActionProvider`` interface (``actionmesh.providers.base``).

Everything shipped so far is ``simulated = True`` and is labelled MOCK. That
flag propagates into execution results so a simulated run is never reported as
a real one.

``default_mock_providers()`` returns the C7 domain providers in a fixed,
most-specific-first order, with the generic fallback last:

    messaging → calendar → notifications (+ contacts) → files →
    devices → search → payments → system → generic fallback
"""

from __future__ import annotations

from actionmesh.protocol.models import Action
from actionmesh.providers.base import (
    ActionProvider,
    ProviderError,
    ProviderResult,
)
from actionmesh.providers.calendar import MockCalendarProvider
from actionmesh.providers.files import MockFileProvider
from actionmesh.providers.messaging import MockMessagingProvider
from actionmesh.providers.misc import (
    MockDeviceProvider,
    MockSearchProvider,
    MockSystemProvider,
)
from actionmesh.providers.mock_base import MockHarnessMixin
from actionmesh.providers.notifications import (
    MockContactsProvider,
    MockNotificationProvider,
)
from actionmesh.providers.payments import MockPaymentProvider
from actionmesh.providers.real_base import IntegrationConfig, RealProviderBase


class MockProvider(MockHarnessMixin, ActionProvider):
    """Generic deterministic fallback (pre-C7 behaviour, preserved).

    Supports every ``provider="mock"`` action the domain providers do not
    claim. Last in ``default_mock_providers()`` so specific providers win.
    Keeps all the pre-C7 test behaviour: ``state``, ``calls``, ``failures``,
    ``verify_failures``, ``flaky``, deterministic receipts, ``observe()``.
    """

    name = "mock"
    SUPPORTED = frozenset()  # generic: supports anything addressed to "mock"

    def supports(self, action: Action) -> bool:
        return action.provider in ("mock", self.name) or not action.provider


def default_mock_providers() -> list[ActionProvider]:
    """The C7 provider set. Most-specific first, generic fallback last."""
    return [
        MockMessagingProvider(),
        MockCalendarProvider(),
        MockNotificationProvider(),
        MockFileProvider(),
        MockDeviceProvider(),
        MockSearchProvider(),
        MockPaymentProvider(),
        MockSystemProvider(),
        MockProvider(),
    ]


__all__ = [
    "ActionProvider",
    "IntegrationConfig",
    "MockCalendarProvider",
    "MockContactsProvider",
    "MockDeviceProvider",
    "MockFileProvider",
    "MockHarnessMixin",
    "MockMessagingProvider",
    "MockNotificationProvider",
    "MockPaymentProvider",
    "MockProvider",
    "MockSearchProvider",
    "MockSystemProvider",
    "ProviderError",
    "ProviderResult",
    "RealProviderBase",
    "default_mock_providers",
]


