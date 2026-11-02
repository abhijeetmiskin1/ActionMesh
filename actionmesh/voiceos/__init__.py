"""VoiceOS — the reasoning and task layer that sits on top of ActionMesh.

Nothing in this package can authorise, approve or execute anything by itself. It holds
*conversation and task state*: what the user asked, what was proposed, which step is
next, and what the executor actually reported. Authority stays where it has always been
— the registry, the consent store, the policy engine and the human pressing approve.

The split that matters:

* :mod:`actionmesh.voiceos.models` / :mod:`~actionmesh.voiceos.store` — sessions and
  their steps, persisted, scoped to the actor that asked.
* :mod:`actionmesh.voiceos.tasks` — operator-authored task templates. A multi-step plan
  for "run my tests" needs a test command, and the only honest sources for one are the
  user's own words and the operator's configuration. The model gets neither vote.
* :mod:`actionmesh.voiceos.llm` — the provider seam, its configuration and its refusal
  to pretend. There is no approved outbound transport in this build, so a remote
  provider name resolves to *no proposal plus a stated reason*, never to a guess.
* :mod:`actionmesh.voiceos.reasoning` — intent, conversational references and
  decomposition into steps.
* :mod:`actionmesh.voiceos.orchestrator` — runs one step at a time **inside the
  caller's own request**, so every step is authorised against the caller's real
  credential, and re-authorised on every continuation.
"""

from actionmesh.voiceos.models import TaskSession, TaskStep
from actionmesh.voiceos.orchestrator import Orchestrator
from actionmesh.voiceos.reasoning import Reasoner, Understanding
from actionmesh.voiceos.store import VoiceOsStore
from actionmesh.voiceos.tasks import TaskTemplate, load_templates

__all__ = [
    "Orchestrator",
    "Reasoner",
    "TaskSession",
    "TaskStep",
    "TaskTemplate",
    "Understanding",
    "VoiceOsStore",
    "load_templates",
]
