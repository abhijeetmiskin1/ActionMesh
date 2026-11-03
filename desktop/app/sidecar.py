/**
 * The same FastAPI application the dashboard serves, started as a desktop sidecar.
 *
 * Nothing is re-implemented here: this is a launcher, so that a packaged app can
 * start its managed backend without a system Python and without a shell script the
 * installer has to guess at. The startup order the app documents — directories,
 * configuration, credentials, backend, agent, /ready — is enforced by `main.js`,
 * which sets the environment before this process exists.
 *
 * Arguments: ``--host`` and ``--port`` only. Everything else is environment, so the
 * process table cannot be used to read or alter the configuration.
 */
import argparse
import sys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="actionmesh-sidecar")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    try:
        import uvicorn
    except ImportError:
        print(
            "the desktop runtime is missing uvicorn (expected in the bundled runtime)",
            file=sys.stderr,
        )
        return 2

    # Refuse to serve off-loopback. The desktop shell is the loopback boundary's
    # beneficiary, not a reason to widen it; a sidecar bound to 0.0.0.0 would expose
    # the machine's agent to the network with only a key between it and the room.
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        print(
            f"refusing to bind {args.host!r}: the desktop sidecar is loopback-only",
            file=sys.stderr,
        )
        return 2

    uvicorn.run(
        "actionmesh.api:app",
        host=args.host,
        port=args.port,
        log_level="info",
        access_log=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())