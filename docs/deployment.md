# Deployment

The release image runs the FastAPI application with Uvicorn:

```bash
docker build -t actionmesh .
docker run --rm -p 8080:8080 \
  -e ACTIONMESH_API_KEY=replace-me \
  -e ACTIONMESH_AUDIT_DB=/data/actionmesh-audit.db \
  actionmesh
```

Check the process with `GET /health`. The dashboard is at `/dashboard`,
`actionmesh capabilities --provider mock` lists the seeded actions locally, and
`POST /plan` turns intent into an untrusted candidate plan without running
anything.

## Deployment modes

`ACTIONMESH_ENV` selects one of three modes, and the difference is enforced at
startup, not just documented:

- **`development`** (the default) — in-memory execution/consent/idempotency state,
  an ephemeral signing key, no API key. Correct on your own laptop; it is *not* a
  server. Nothing is required, so a developer is never blocked by production config.
- **`lan`** — development plus a required `ACTIONMESH_API_KEY`, for a phone talking to
  a laptop across a room. Transport may be cleartext HTTP; it must never be presented
  as a secure internet deployment.
- **`production`** — refuses to start unless the state that is supposed to survive a
  restart actually will. Required: durable execution storage
  (`ACTIONMESH_DATABASE_URL`), a durable audit chain (`ACTIONMESH_AUDIT_DB`), a
  durable signing key (`ACTIONMESH_SECRET_KEY` or `ACTIONMESH_SECRET_FILE`), a shared
  `ACTIONMESH_API_KEY`, and a `ACTIONMESH_PUBLIC_BASE_URL`. If it binds to a
  non-loopback address it also requires `ACTIONMESH_TRUSTED_PROXIES`, because
  otherwise the client address used for any future rate limiting is one an attacker
  can forge.

Startup validation raises a single `ConfigError` listing **every** gap, so an
operator fixes them in one pass rather than one restart at a time. There is no path
where a missing production safeguard quietly falls back to a development default.

## Configuration

Copy `.env.example` to a local environment file and provide secrets through the
runtime environment or a secret manager. Do not bake `.env` into an image.

| Variable | Effect |
| --- | --- |
| `ACTIONMESH_ENV` | `development` (default) · `lan` · `production`. See above. |
| `ACTIONMESH_HOST` / `ACTIONMESH_PORT` | Bind address and port (default `127.0.0.1:8000`). |
| `ACTIONMESH_PUBLIC_BASE_URL` | Required in production; the externally visible URL. |
| `ACTIONMESH_API_KEY` | Enables the transport guard. Required in `lan`/`production`. |
| `ACTIONMESH_DATABASE_URL` | Durable execution store (`sqlite:///…`). Required in production. |
| `ACTIONMESH_AUDIT_DB` | Persists the audit chain to SQLite. It **reopens fail-closed**: tampered history stops startup. |
| `ACTIONMESH_AUDIT_ANCHOR` | Adds tail-truncation detection (see [`audit.md`](audit.md)). Point it at storage the audit database cannot rewrite, or it buys nothing. |
| `ACTIONMESH_SECRET_KEY` / `ACTIONMESH_SECRET_FILE` | Durable signing key for pairing/credentials. Without one the key is ephemeral and a restart unpairs every device. |
| `ACTIONMESH_TRUSTED_PROXIES` | Comma-separated proxy addresses whose `Forwarded` headers may be trusted. Required in production when bound off-loopback. |
| `ACTIONMESH_MAX_REQUEST_BYTES` | Request body ceiling (minimum 1024). |
| `ACTIONMESH_RATE_LIMIT` | Process-local sliding-window limiter on the auth-sensitive and execution endpoints (default on; `0` disables for local dev/tests). It is per-process — a multi-process or relayed deployment must enforce the same quota at the proxy. |
| `ACTIONMESH_ALLOW_REMOTE_PAIRING` | Deliberately widens code minting beyond loopback. Off by default. |

## Backup and recovery

The durable state is two SQLite files — the execution store (`ACTIONMESH_DATABASE_URL`)
and the audit chain (`ACTIONMESH_AUDIT_DB`) — plus, if used, the anchor and the signing
key file. Back them up together: an audit chain restored without its anchor cannot
prove it was not truncated, and a database restored without the signing key unpairs
every device. Use `sqlite3 <db> ".backup <dest>"` (not a file copy under load) and
restore to a **separate** location, then run `GET /audit/verify` and confirm the chain
before trusting a restored audit file — a file that exists is not a file that
verifies. The signing key and database secrets must not be committed or bundled into
source backups.

The default API process uses in-memory execution, event, consent, and
idempotency state. SQLite-backed stores are available for deployments that
construct an `ExecutionEngine` with `SQLiteStore`, `SQLiteConsentStore`, and
`SQLiteIdempotencyLedger`; configure backups and retention before treating that
state as durable production data.

## Local ASGI run

Install the optional server extra and run:

```bash
pip install ".[server]"
actionmesh serve --host 127.0.0.1 --port 8080
```

## Android client

`python scripts/build_apk.py` builds a signed APK to
`dist/actionmesh-<version>.apk` — a WebView shell around the same dashboard,
signed with a **local debug keystore**. Sideloading is fine for testing; a store
release needs its own key held outside the repository. See
[`../android/README.md`](../android/README.md).

No live vendor integrations are enabled by this image. The C13 integration
boundary requires HTTPS endpoints, environment-managed credentials, and
concrete scopes; all seeded actions still use simulated providers until an
official adapter is selected and implemented.
