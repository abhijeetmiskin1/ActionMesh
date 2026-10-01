# Providers

Providers are the only boundary where an action may produce a side effect. The
core executor depends on the small asynchronous `ActionProvider` interface and
does not contain domain-specific provider behavior.

## Mock providers

All shipped providers are deterministic in-memory simulators. They expose
`simulated = True`, record idempotent receipts by `idempotency_key`, and expose
`observe()` records containing the action type, idempotency key, receipt id, and
parameters. The verification engine uses those records as independent evidence.

The default provider order is most-specific first, with `MockProvider` last as a
fallback:

- `MockMessagingProvider`: `message.send`, `message.read`
- `MockCalendarProvider`: `calendar.create`, `calendar.update`, `calendar.delete`
- `MockNotificationProvider`: `reminder.create`, `device.notify`
- `MockFileProvider`: `file.read`, `file.move`, `file.share`
- `MockDeviceProvider`: `device.send_file`
- `MockSearchProvider`: `search.web`
- `MockPaymentProvider`: `payment.prepare`, `payment.confirm`, `payment.verify`
- `MockSystemProvider`: `system.launch_app`
- `MockContactsProvider`: read-only lookup helper; it does not execute actions

A domain provider only claims actions addressed to `mock` or its own provider
name. The generic provider remains available for seeded or experimental mock
actions that have no specialized implementation.

## Payments

`MockPaymentProvider` models an explicit lifecycle:

```text
prepared -> confirmed -> settled
                     -> failed
```

`settle(payment_id)` and `fail(payment_id)` are provider lifecycle operations,
not registry actions. A declined instrument raises `AUTHORIZATION`; a
`gateway_down` recipient raises `PROVIDER`. Neither path performs network I/O
or moves real money.

## Extending a provider

Subclass `MockHarnessMixin` and `ActionProvider`, declare `name` and
`SUPPORTED`, implement side effects in `_handle()`, and preserve the shared
receipt and observation contract. Do not perform network calls, read
credentials, or mutate state from `supports()`.

Before implementing an official adapter, use the vendor-neutral
[provider contract](provider-contract.md) and
[requirements matrix](provider-requirements.md). The evidence collected so far
is recorded in the [provider-readiness evidence matrix](provider-evidence.md).
The [decision-readiness matrix](provider-decision-readiness.md) separates
contract compatibility, runtime compatibility, and product suitability. These
documents are gates for provider selection, not a commitment to any vendor or
external side effect.
