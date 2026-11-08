# Security Policy

## Supported versions

ActionMesh is pre-1.0. Security fixes are applied to the latest revision only.

| Version | Supported |
|---------|-----------|
| 0.1.x   | yes       |
| < 0.1   | no        |

## Reporting a vulnerability

Please report security issues privately rather than opening a public issue.

Use GitHub's **"Report a vulnerability"** button on the Security tab of the
repository. If that is unavailable, open an issue marked as non-public to the
maintainers, describing only the impact and how to reproduce it.

Please include:

```text
what an attacker can achieve
the minimum input needed to reproduce it
the ActionMesh version and Python version
whether the issue is reachable without real credentials
```

You should get an acknowledgement within a few days. Please give the maintainers
a reasonable window to ship a fix before disclosing publicly.

## What counts as a vulnerability here

In scope:

- Bypassing the permission or policy engine — for example, causing an action to
  execute when the policy says `DENY` or when approval was never given.
- The LLM being able to grant itself a permission, lower its own risk, or set
  its own lifecycle state.
- A wildcard or expired consent authorizing an action.
- Injection through a trust boundary (untrusted graph, plan, or parameters).
- Exposure of a secret, token or credential.
- A graph being able to contain a cycle, or otherwise stall the executor.

Out of scope as product gaps rather than vulnerabilities:

- Missing production identity, key rotation, rate limiting, TLS termination,
  or vendor integrations. The API, CLI, dashboard, and SQLite foundations do
  exist, but they are not a substitute for deployment-specific controls. See
  [`docs/security.md`](docs/security.md).
- Findings that require an attacker who already has code execution on the host.

## Non-negotiable rules

These are architectural, not policy, and changes to them should be treated as
security-sensitive:

```text
AI proposes.  Policy decides.  User controls.
Executor acts.  Verifier confirms.  Audit remembers.
```

- The LLM output is **untrusted data** at all times.
- Authorization lives in exactly one place: `PolicyEngine`.
- A `DENY` is terminal.
- Risk may be raised by trusted metadata, never lowered.
- Wildcard consent is not offered.
- Missing or malformed context fails **closed**.
- Secrets come from the environment or a secret store — never from source, logs,
  audit records or API responses.
- The HTTP boundary uses constant-time API-key comparison when configured,
  rejects oversized declared request bodies, and sets anti-sniffing, frame, and
  referrer headers. These controls are defense in depth, not a replacement for
  a production identity provider, rotation, rate limiting, or TLS termination.
