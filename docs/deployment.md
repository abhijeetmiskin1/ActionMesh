# Deployment

The release image runs the FastAPI application with Uvicorn:

```powershell
docker build -t actionmesh .
docker run --rm -p 8080:8080 `
  -e ACTIONMESH_API_KEY=replace-me `
  actionmesh
```

Check the process with `GET /health`. The dashboard is at `/dashboard`, and
`actionmesh capabilities --provider mock` lists the seeded actions locally.

## Configuration

Copy `.env.example` to a local environment file and provide secrets through the
runtime environment or a secret manager. Do not bake `.env` into an image. Set
`ACTIONMESH_API_KEY` for the optional transport guard; production deployments
should place a full identity provider and rate limiter in front of the service.

The default API process uses in-memory execution, event, consent, and
idempotency state. SQLite-backed stores are available for deployments that
construct an `ExecutionEngine` with `SQLiteStore`, `SQLiteConsentStore`, and
`SQLiteIdempotencyLedger`; configure backups and retention before treating that
state as durable production data.

## Local ASGI run

Install the optional server extra and run:

```powershell
pip install ".[server]"
actionmesh serve --host 127.0.0.1 --port 8080
```

No live vendor integrations are enabled by this image. The C13 integration
boundary requires HTTPS endpoints, environment-managed credentials, and
concrete scopes; all seeded actions still use simulated providers until an
official adapter is selected and implemented.
