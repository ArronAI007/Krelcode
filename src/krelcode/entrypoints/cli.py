"""Lightweight process entrypoint.

This module mirrors the fast-path layer used by large coding-agent CLIs:

* only the standard library and the tiny package module are loaded for
  ``--version``;
* the complete CLI is imported lazily for every other invocation;
* the original argv is forwarded unchanged, so argument semantics stay owned
  by the complete parser.

Heavy Agent, Provider, MCP, Sandbox and UI modules must never be imported here.
"""

from __future__ import annotations

from collections.abc import Sequence
from importlib import import_module
import sys


def main(argv: Sequence[str] | None = None) -> int:
    """Run Krelcode through the process-level fast/full dispatch boundary."""
    args = list(sys.argv[1:] if argv is None else argv)

    # Keep this path independent from project/global configuration parsing.
    if args and args[0] == "--version":
        from krelcode import __version__

        print(f"krelcode {__version__}")
        return 0

    # Lazy import is intentional: ordinary command help and non-Agent
    # subcommands should not pull in the composition root before they exist.
    full_main = import_module("krelcode.entrypoints.main").main
    return int(full_main(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
