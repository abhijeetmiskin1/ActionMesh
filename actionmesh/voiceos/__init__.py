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
* :mod:`actionmesh.voiceos.llm` — the provider seam, its configuration, and its refusal
  to pretend. One provider is wired (Anthropic, through its official Messages endpoint),
  and it is only built when a credential is present in the environment; without one the
  seam returns *no proposal plus a stated reason*, never a guess. A model-backed planner
  wraps the deterministic one, so every failure path is answered by the rules and says
  which planner replied.
* :mod:`actionmesh.voiceos.transport` — the package's single outbound HTTPS call. The
  destination is a closed list inside that module, it speaks to port 443 with verified
  TLS, bounds both directions by size, follows no redirects and surfaces no response
  body. It is granted to one file by the security guard, as a member call rather than a
  module-import exemption, so nothing else — the execution engine included — gains
  network access.
* :mod:`actionmesh.voiceos.reasoning` — intent, conversational references and
  decomposition into steps.
* :mod:`actionmesh.voiceos.orchestrator` — runs one step at a time **inside the
  caller's own request**, so every step is authorised against the caller's real
  credential, and re-authorised on every continuation.

Automations — a stored plan, and the ticker that fires it:

* :mod:`actionmesh.voiceos.workflows` — the stored shape, its validation and its
  schedule arithmetic. A workflow has no field a grant could live in, and every value
  that reaches it comes from a closed list: known actions, published event types, the
  executor's own step states, integer weekdays and ``HH:MM`` clocks.
* :mod:`actionmesh.voiceos.workflow_draft` — natural language to a *proposal*. It may
  recognise a schedule; it may never emit a step. Steps come only from an
  operator-authored template, so a sentence cannot introduce an action.
* :mod:`actionmesh.voiceos.automation_store` — durable workflows and runs. The
  ``(workflow_id, slot)`` unique constraint is what makes an occurrence run at most
  once, and ``reconcile_claimed`` turns a claim whose process died into ``missed``
  rather than a silent re-run.
* :mod:`actionmesh.voiceos.automation_run` — turns a plan into an ordinary task
  session and hands it to the orchestrator. A run nobody pressed carries the owner's
  identity and **no grants by construction**, so a schedule cannot authorise anything.
* :mod:`actionmesh.voiceos.automation_scheduler` — one local ``asyncio`` task. No
  cloud, no daemon. It advances the schedule before running, records an occurrence
  late by more than the grace window as ``missed`` instead of running it late, and
  matches an event by type and recorded state only — never by its payload.
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
