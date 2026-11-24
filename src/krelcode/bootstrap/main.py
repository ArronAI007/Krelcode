"""Process lifecycle owner for the full application bootstrap."""

from __future__ import annotations

import argparse

from .application import BootstrappedApplication, create_application


class AsyncMain:
    """Build, execute, and close one process-scoped application graph.

    Provider selection, policy loading, MCP/plugin wiring, and session routing
    intentionally live outside this class.  ``AsyncMain`` owns only the
    lifecycle boundary so alternative hosts can reuse the same application
    graph without inheriting process concerns.
    """

    async def run(self, args: argparse.Namespace) -> int:
        bootstrapped = await create_application(args)
        if isinstance(bootstrapped, int):
            return bootstrapped
        application: BootstrappedApplication = bootstrapped
        try:
            return await application.run()
        finally:
            await application.close()


async def async_main(args: argparse.Namespace) -> int:
    """Compatibility coroutine used by the lazy CLI entrypoint."""
    return await AsyncMain().run(args)
