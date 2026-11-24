"""Internal daemon process entrypoint (`python -m krelcode.daemon`)."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Sequence

from ..app import build_daemon_agent_runtime
from .server import run_daemon


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m krelcode.daemon")
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--config", default=None)
    parser.add_argument("--idle-timeout", type=float, default=1_800.0)
    parser.add_argument("--sandbox-backend", default=None)
    parser.add_argument("--sandbox-network", action="store_true", default=None)
    parser.add_argument("--no-sandbox", action="store_true")
    parser.add_argument("--auto-approve", action="store_true")
    parser.add_argument("--permission-mode", default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    asyncio.run(run_daemon(
        Path(args.workspace).expanduser().resolve(),
        config_path=Path(args.config).expanduser() if args.config else None,
        idle_timeout=args.idle_timeout,
        sandbox_backend=args.sandbox_backend,
        sandbox_network=args.sandbox_network,
        no_sandbox=args.no_sandbox,
        auto_approve=args.auto_approve,
        permission_mode=args.permission_mode,
        agent_runtime_factory=build_daemon_agent_runtime,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
