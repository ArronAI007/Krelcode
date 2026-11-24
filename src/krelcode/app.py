"""Backward-compatible application facade.

The concrete composition helpers now live in :mod:`krelcode.bootstrap`.
Existing embedders may continue importing the historical symbols from this
module; the process lifecycle itself is owned by ``bootstrap.main.AsyncMain``.
"""

from __future__ import annotations

import argparse
from typing import Any, Callable, Dict, Optional

from .bootstrap.composition import *  # noqa: F401,F403 - compatibility facade
from .bootstrap.composition import (
    build_subagent_llm_resolver as _build_subagent_llm_resolver,
    create_provider,
    resolve_settings,
)
from .bootstrap.main import AsyncMain, async_main
from .core.redact import SecretRedactor
from .enterprise.policy import EnterprisePolicy
from .llm.base import LLMProvider


def build_subagent_llm_resolver(
    args: argparse.Namespace,
    config: Dict[str, Any],
    parent: LLMProvider | Callable[[], LLMProvider],
    redactor: Optional[SecretRedactor] = None,
    enterprise_policy: Optional[EnterprisePolicy] = None,
) -> Callable[[str], LLMProvider]:
    """Compatibility wrapper that honors module-level factory patches."""
    return _build_subagent_llm_resolver(
        args,
        config,
        parent,
        redactor,
        enterprise_policy,
        settings_factory=resolve_settings,
        provider_factory=create_provider,
    )


def main(argv=None) -> int:
    """Backward-compatible delegator to the process entrypoint."""
    from .entrypoints.cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
