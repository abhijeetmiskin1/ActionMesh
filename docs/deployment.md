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

## Configuration

Copy `.env.example` to a local environment file and provide secrets through the
runtime environment or a secret manager. Do not bake `.env` into an image. Set
`ACTIONMESH_API_KEY` for the optional transport guard; production deployments
should place a full identity provider and rate limiter in front of the service.

| Variable | Effect |
| --- | --- |
| `ACTIONMESH_API_KEY` | Enables the transport guard. Without it the API is open. |
| `ACTIONMESH_AUDIT_DB` | Persists the audit chain to SQLite. It **reopens fail-closed**: tampered history stops startup. |
| `ACTIONMESH_AUDIT_ANCHOR` | Adds tail-truncation detection (see [`audit.md`](audit.md)). Point it at storage the audit database cannot rewrite, or it buys nothing. |
| `ACTIONMESH_MAX_REQUEST_BYTES` | Request body ceiling (minimum 1024). |

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
