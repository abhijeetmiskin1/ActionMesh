"""The laptop agent package — the only part of ActionMesh that touches a machine.

Everything under this directory is the **execution adapter** the architecture puts
below the policy engine: it receives an already-authorized ActionMesh action and
carries it out on the local filesystem, process table, or editor. It never decides
whether an action is allowed, and the layout is chosen to make that auditable — the
modules that need ``subprocess`` are named individually on the dangerous-primitive
guard's allowlist, so "what can run programs on this laptop" is a two-file question
rather than a package-wide one.

Read the pieces in the order they constrain each other:

``config``
    what the operator said this agent may reach, and therefore which capabilities it
    is allowed to advertise.
``definitions``
    the device actions themselves — risk levels and consent scopes fixed here, not
    by any caller.
``paths``
    the workspace root and the confinement every file operation resolves through.
``commands``
    the ALLOWED / ASK / DENIED command policy that decides which programs may run at
    all, before a provider is ever reached.
``runner``
    the only place a process is spawned. Argument vectors, never a shell string.
``provider``
    the ``ActionProvider`` the executor dispatches to.
``presence``
    :class:`~actionmesh.agent.presence.LaptopAgent` — registration and heartbeat.
"""

from __future__ import annotations

from actionmesh.agent.config import (
    DEFAULT_HEARTBEAT_S,
    AgentConfig,
    AgentConfigError,
)
from actionmesh.agent.definitions import (
    CAPABILITIES,
    PROVIDER,
    device_definitions,
    register_device_actions,
)
from actionmesh.agent.presence import LaptopAgent, resources

__all__ = [
    "CAPABILITIES",
    "DEFAULT_HEARTBEAT_S",
    "PROVIDER",
    "AgentConfig",
    "AgentConfigError",
    "LaptopAgent",
    "device_definitions",
    "register_device_actions",
    "resources",
]
