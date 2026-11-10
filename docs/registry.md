# Action Registry (C2)

The registry is a **description layer**. It answers "what actions exist, what do
they require, and are these parameters valid?" It never executes an action,
never calls a provider, and never decides authorization.

That last point is deliberate: the registry describes, the policy engine decides.

## Contract

```text
register(defn)         validate metadata, reject duplicate (name, version)
get(name, version?)    latest by default; unknown name/version raises
versions(name)         sorted numerically (1.10 > 1.9)
list()                 all definitions, name asc then version asc
discover(...)          filter by name / provider / minimum risk / permission
resolve(raw)           bind an untrusted action to its definition
```

## Definition validation

`validate_definition` rejects a definition that is not well-formed:

```text
name             non-empty string
version          non-empty, contains a numeric component
risk_level       a RiskLevel
timeout_s        > 0
permissions      every scope known AND concrete (no "*", no "ns.*")
input/output     objects; properties is an object; required ⊆ properties
retry_policy     valid backoff, max_attempts ≥ 1
```

An invalid definition cannot be registered at all — the registry stays empty
rather than half-populated.

## Parameter checking

`check_params` compares untrusted parameters against `input_schema`:

- a missing **required** parameter → `ValidationError`
- a value of the **wrong type** → `ValidationError`
- an **unrecognized** parameter → a warning, not a failure
- an **untyped** property → a warning, value passed through as opaque data
- a **malformed property spec** (not an object, or an unknown type) →
  `DefinitionError`, i.e. **fail closed**

That last case is the important one. Silently skipping a schema the registry
cannot read would let arbitrary input through under the appearance of
validation.

## Resolution

```python
resolved = registry.resolve({
    "type": "message.send",
    "parameters": {"message": "I'll be late"},
    "risk_level": "low",          # ignored — see below
    "idempotency_key": "k1",      # preserved
})
resolved.definition            # ActionDefinition
resolved.effective_risk        # MEDIUM, not LOW
resolved.warnings              # ["risk lifted low -> medium (never lowered)"]
```

Order matters and is deliberate:

1. **Shape-validate** the untrusted action. Malformed input never becomes an
   `Action`.
2. **Reset lifecycle state** to `CREATED`. Untrusted input cannot claim an
   action already succeeded.
3. **Look up** the definition. Unknown action → `UnknownActionError`.
4. **Check parameters.** Missing/wrong type → `ValidationError`.
5. **Raise risk** to `max(action, definition)`. Never lower it.

`action_id` is system-assigned; `idempotency_key` is preserved from the caller
so a retry of the same logical request can be deduplicated by the executor.

## Discovery

`discover()` returns `Capability` objects (not definitions) and reports only the
**latest** version of each action — someone asking "what can I do?" wants
current capabilities, not historical ones.

```python
registry.discover()                          # 16 capabilities
registry.discover(provider="mock")           # 16
registry.discover(risk=RiskLevel.CRITICAL)   # payment.confirm, payment.prepare
registry.discover(permission="message.send") # message.send
```

## Seeded capabilities

16 `provider="mock"` definitions ship for local development. Every description
is prefixed `MOCK:`. **They perform no side effect** — they describe shape only.

```text
message.send      message.read
calendar.create   calendar.update   calendar.delete
reminder.create                       (implemented on the calendar surface)
file.read         file.move         file.share
device.notify     device.send_file
payment.prepare   payment.confirm   payment.verify     (CRITICAL)
search.web        system.launch_app
```

`payment.prepare` and `payment.confirm` are `CRITICAL` and require
`payment.execute` / `payment.prepare` respectively. There is no
`payment.execute` *action* — the action is `payment.confirm`.

## Adding a capability

```python
registry.register(ActionDefinition(
    name="acme.report.build",
    version="1.0",
    description="MOCK: build a report",
    risk_level=RiskLevel.MEDIUM,
    permissions=["file.read"],
    input_schema={
        "type": "object",
        "properties": {"template": {"type": "string"}},
        "required": ["template"],
    },
    provider="mock",
))
```

A capability that names an unknown permission, or a wildcard, is rejected. The
catalog is the authority on what may be required.
