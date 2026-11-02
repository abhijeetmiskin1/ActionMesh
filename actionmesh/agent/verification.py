"""C6 verification strategies for device capabilities.

A device action with no registered strategy is reported ``UNSUPPORTED`` — which is
C6's correct fail-closed answer, and would make every laptop capability permanently
unconfirmable. So each implemented capability is registered here with the same
``observed_state`` strategy the seeded mocks use: the verifier calls
``provider.observe()`` and reads the machine again, rather than accepting what the
code that changed it says happened.

Registration lives next to the capability list rather than in
``core/verification/engine.py`` on purpose. The core's ``default_engine()`` describes
what the *mock* seeds must be provable about; a real adapter declaring its own
verifiability keeps that honest without teaching the core about laptops.
"""

from __future__ import annotations

from actionmesh.agent.definitions import IMPLEMENTED
from actionmesh.core.verification.engine import VerificationEngine

#: Every implemented device capability is confirmed by re-reading the effect.
STRATEGY = "observed_state"


def register_device_strategies(engine: VerificationEngine) -> int:
    """Declare how each device capability is verified. Returns how many were new.

    Reads :data:`~actionmesh.agent.definitions.IMPLEMENTED`, so a capability that is
    defined but not implemented cannot become verifiable — there would be nothing for
    the verifier to observe, and registering a strategy for it would claim a check that
    could never run.
    """
    added = 0
    for action_type in sorted(IMPLEMENTED):
        if engine.strategy_for(action_type) == STRATEGY:
            continue
        engine.register(action_type, STRATEGY)
        added += 1
    return added


__all__ = ["STRATEGY", "register_device_strategies"]
