# Permissions & Policy

The authorization boundary of ActionMesh.

> **The LLM proposes actions but never grants itself permission.**
> Only `PolicyEngine` produces a decision, and it is a pure function.

## Model

### Permissions

A permission is a granular, machine-readable scope such as `message.send`.
The catalog is **closed** — 20 scopes, in
`actionmesh/core/permissions/catalog.py`:

```text
calendar.read   calendar.write   calendar.delete
message.read    message.send
file.read       file.write       file.share       file.delete
device.notify   device.file_transfer             device.control
device.register  device.file.list  device.project.read  device.process.read
device.process.control  device.server.read  device.server.control
device.ide.open
location.read
payment.prepare payment.execute  payment.verify
system.app.launch   system.shell
reminder.write      search.web
```

Because the set is closed, "unknown permission" is a *detectable* condition and
can therefore fail closed. An open namespace would make typos silent.

The `device.*` rows added with C19 are the scopes the **laptop agent** acts on:
`device.register` and the read-only presence/inspection scopes are LOW, process and
server control are HIGH, and the editor scope is MEDIUM. What they are *not* is a
capability list — a paired phone holds no scope until a consent names it, and
`device.terminal.execute` still asks for `system.shell`, the CRITICAL scope it has
always had.

### Grants vs. rules — the distinction that matters

These are matched differently, and conflating them is the most common way a
permission system gets compromised.

| | Who writes it | Wildcards? | Match |
|---|---|---|---|
| **Grant** | the user, via `ConsentStore` | **never** | exact string equality |
| **Policy rule** | the operator, in config | allowed (`message.*`, `*`) | pattern match |

A grant may never be a wildcard. Consenting to `message.*` would silently
authorize `message.send` today *and* every message capability added next year.
`ConsentStore.grant` refuses wildcard scopes outright, and
`scope_satisfied("message.*", "message.send")` is `False`.

Wildcards in *policy rules* are fine: they are trusted configuration, and when
two rules match equally specifically the engine resolves to the **most
restrictive** decision anyway.

### Risk

Four levels — `LOW`, `MEDIUM`, `HIGH`, `CRITICAL`. Risk is not cosmetic; it
gates authorization directly.

```text
calendar.create    LOW       message.send        MEDIUM
file.share         HIGH      payment.confirm     CRITICAL
```

Two rules govern it:

1. **Risk is only ever raised, never lowered.** Effective risk is
   `max(action.risk_level, definition.risk_level)`. Submitting
   `"risk_level": "low"` for a CRITICAL action changes nothing.
2. **Each grant carries its own `max_risk` ceiling.** A grant capped at `HIGH`
   cannot authorize a CRITICAL action, whatever the policy says.

## Decision precedence

`PolicyEngine.evaluate` is deterministic — the same context always yields the
same outcome. The first decisive step wins.

1. **Structural preconditions → DENY.** Missing action, missing definition,
   definition/action name mismatch, missing actor, or a definition that
   declares no permissions.
2. **Compute effective risk.** `max(action, definition)`. Record if it was lifted.
3. **Collect requirements.** Definition permissions, plus any a matched rule
   adds. Every requirement must be *concrete* — a wildcard requirement is a
   DENY. Match each **exactly** against active grants, honouring expiry and the
   grant's `max_risk` ceiling. Any missing requirement → **DENY**.
4. **Select a rule.** Highest specificity wins: `exact` > `namespace.*` > `*`.
   Equally specific rules resolve to the **most restrictive** decision.
5. **No matching rule → `policy.default_decision`**, which is **DENY**.
6. **Risk gate.** `CRITICAL` always becomes `ASK`. `HIGH` becomes `ASK` unless
   the context sets `allow_high_risk` — a **server-side-only** flag, never read
   from a request body.
7. **Approval.** `ASK` is satisfiable: if a real approved `ApprovalRequest`
   exists for this `action_id`, the decision becomes `ALLOW`. Otherwise the
   action waits.



### Decisions

| Decision | Meaning | Executes? |
|----------|---------|-----------|
| `ALLOW` | permitted now | yes |
| `ALLOW_WITH_LIMIT` | permitted within stated limits | yes, within limits |
| `ASK` | permitted **pending user approval** | not until approved |
| `DENY` | refused | never |

`ASK` is *not* a soft deny — it is a pause at the human gate. `DENY` is
terminal: no approval can upgrade it, and a test covers that.

`deny_without_confirmation` and `confirm` are accepted spellings that normalize
to `ASK`. Expressing "no without a human" as a distinct decision would create
two overlapping ways to say the same thing, and they would eventually disagree.

## Policy examples

```yaml
# Trusted configuration. Malformed entries become DENY, never get dropped.
rules:
  - action: calendar.create
    decision: allow
  - action: message.*          # wildcard is fine in a rule
    decision: ask
  - action: payment.confirm
    decision: deny_without_confirmation   # normalizes to ASK
default_decision: deny          # unlisted actions are never auto-allowed
```

## Example

`message.send` is `MEDIUM` and requires `message.send`:

```python
ctx = build_context(
    action, definition, Identity(id="user_1"),
    policy=default_policy(),
    grants=[PermissionGrant(scope="message.send", max_risk=RiskLevel.HIGH)],
)
engine.evaluate(ctx)
# PolicyOutcome(decision=ASK, requires_approval=True, ...)
```

The user consented to messaging. The policy still asks, because sending a
message is a side effect on a third party. Consent to the *capability* is not
consent to *this specific* send.

## Security invariants

Each is covered by a test:

```text
1. Unknown permission fails safely (DENY, never a silent pass).
2. A wildcard grant never satisfies a requirement.
3. Missing actor / definition / action fails closed to DENY.
4. Risk cannot be lowered by the caller, the LLM, or a request body.
5. A grant's max_risk ceiling is enforced.
6. CRITICAL risk always requires explicit approval.
7. A DENY can never be upgraded to ALLOW, not even by an approved request.
8. Expired consent never authorizes.
9. Equally specific rules resolve to the most restrictive decision.
10. Policy evaluation is deterministic and yields an auditable reason list.
```

## Residual risks (honest list)

- **No transport authentication.** There is no API server yet, so
  `authorize_graph` is a library call. Whoever invokes it is trusted; a future
  HTTP layer must add authentication *before* exposing it.
- **`ConsentStore` is in-memory.** Consents do not survive a restart, and there
  is no tamper-evident audit log yet.
- **No separation of duties.** A single actor identity is modelled.
  Multi-user delegation, device identity and revocation are C11.
- **`default_policy()` is a baseline, not a recommendation.** It allows
  reminders and notifications without asking, but still asks before messaging,
  file sharing and anything payment-related.

## Where to look

```text
actionmesh/core/permissions/
├── catalog.py    closed scope list
├── matching.py   exact-grant vs. wildcard-rule matching  ← read this first
├── risk.py       risk ordering; unknown risk ⇒ CRITICAL
├── context.py    PermissionGrant, Consent, PolicyContext, PolicyOutcome
├── policy.py     untrusted config → Policy; default_policy()
├── store.py      server-side grant ledger
└── engine.py     the decision itself; the precedence comment is normative
```
