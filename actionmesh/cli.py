"""C15 command-line entry point."""

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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "capabilities":
        from actionmesh.api import create_app

        registry = create_app().state.engine.registry
        capabilities = registry.discover(provider=args.provider)
        print(json.dumps([capability.model_dump() for capability in capabilities], indent=2))
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

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
