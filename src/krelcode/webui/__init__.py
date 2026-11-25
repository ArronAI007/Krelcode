"""Krelcode Web frontend hosted inside the CLI process lifecycle.

Importing this package does not start the local HTTP server.  Composition is
owned by the process host, keeping the Web UI parallel to the TUI while sharing
the same runtime observability contracts.
"""

__all__: list[str] = []
