"""Declarative application build sequence without process lifecycle ownership."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Callable

from .application_stages import (
    BuildStageError,
    build_application_context,
    build_context_services,
    build_core_services,
    build_extension_services,
    build_interaction_services,
    build_runtime_graph,
    build_session_services,
)
from .composition import RuntimeGraph


@dataclass(frozen=True)
class BuiltRuntime:
    """A completed runtime graph ready to be wrapped by an application host."""

    args: argparse.Namespace
    graph: RuntimeGraph
    note: Callable[[str], None]


async def build_application(
    args: argparse.Namespace,
) -> "BuiltRuntime | int":
    """Run the explicit composition stages in their only valid order."""
    try:
        context = await build_application_context(args)
        interactions = build_interaction_services(context)
        core = await build_core_services(context, interactions)
        context_services = build_context_services(context, core)
        extensions = build_extension_services(
            context, core, context_services)
        session = await build_session_services(context, core)
    except BuildStageError as exc:
        print(exc.message, file=sys.stderr)
        return 2

    graph = build_runtime_graph(
        core=core,
        context=context_services,
        extensions=extensions,
        session=session,
        interactions=interactions,
    )
    return BuiltRuntime(args=context.args, graph=graph, note=context.note)


__all__ = ["BuiltRuntime", "build_application"]
