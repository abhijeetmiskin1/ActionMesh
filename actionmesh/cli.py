"""C15 command-line entry point.

The CLI runs *inside* the package, and the security guard
(``tests/security/test_dangerous_primitives.py``) forbids any outbound network client
in this codebase -- the platform is a server and an offline execution layer, never a
client that dials out. So the operational commands here are the ones that need no
network: configuration validation and diagnostics, and in-process inspection. To act on
a *running* server, use the HTTP API (the dashboard, or an external client you run); to
embed ActionMesh in a program, use :mod:`actionmesh.sdk`, which reaches the same
authoritative state in-process.
"""

from __future__ import annotations

import argparse
import json
import sys


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="actionmesh")
    subcommands = parser.add_subparsers(dest="command", required=True)

    capabilities = subcommands.add_parser("capabilities", help="list registered actions")
    capabilities.add_argument("--provider", default=None)

    dashboard = subcommands.add_parser("dashboard", help="print the dashboard URL")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8000)

    serve = subcommands.add_parser("serve", help="run the FastAPI application")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    config = subcommands.add_parser("config", help="configuration commands")
    config.add_argument("action", choices=["validate"])

    subcommands.add_parser("doctor", help="validate configuration and report the mode")
    return parser


def _emit(value: object) -> None:
    print(json.dumps(value, indent=2, default=str))


def _validate_config() -> int:
    from actionmesh.core.config import AppConfig, ConfigError

    try:
        config = AppConfig.from_env()
    except ConfigError as exc:
        print(f"configuration invalid:\n{exc}", file=sys.stderr)
        return 1
    _emit(
        {
            "mode": config.mode.value,
            "host": config.host,
            "port": config.port,
            "durable_execution": config.durable_execution,
            "durable_audit": config.durable_audit,
            "api_key_configured": bool(config.api_key),
            "valid": True,
        }
    )
    return 0


def _doctor() -> int:
    """Report configuration health without opening a single socket.

    Every check is a fact readable from config or the process; none of them dial out,
    because the guard that keeps this package free of a network client is the same one
    that keeps a compromised laptop from phoning home.
    """
    from actionmesh.core.config import AppConfig, ConfigError
    from actionmesh.core.secret import load_key

    try:
        config = AppConfig.from_env()
    except ConfigError as exc:
        print("config: INVALID", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 1
    print("config: valid")
    print(f"mode: {config.mode.value}")
    print(f"durable_execution: {'yes' if config.durable_execution else 'no (in-memory)'}")
    print(f"durable_audit: {'yes' if config.durable_audit else 'no (in-memory)'}")
    print(f"api_key: {'configured' if config.api_key else 'not set'}")
    # Report the signing key's durability without ever printing the key itself.
    from actionmesh.core.secret import SecretError

    try:
        key = load_key()
        print(f"signing_key: {'durable' if key.durable else 'ephemeral (restart unpairs devices)'}")
    except SecretError as exc:
        print(f"signing_key: ERROR ({exc})", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    if args.command == "capabilities":
        from actionmesh.api import create_app

        registry = create_app().state.engine.registry
        capabilities = registry.discover(provider=args.provider)
        _emit([capability.model_dump() for capability in capabilities])
        return 0

    if args.command == "dashboard":
        print(f"http://{args.host}:{args.port}/dashboard")
        return 0

    if args.command == "serve":
        try:
            import uvicorn
        except ImportError:
            print(
                "Serving requires the optional server dependency: "
                "pip install 'actionmesh[server]'",
                file=sys.stderr,
            )
            return 2
        uvicorn.run("actionmesh.api:app", host=args.host, port=args.port)
        return 0

    if args.command == "config":
        return _validate_config()

    if args.command == "doctor":
        return _doctor()

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
