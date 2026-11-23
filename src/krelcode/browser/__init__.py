"""Safe, bounded browser sessions for optional page automation.

The static backend is always available and only performs read-only HTML/JSON
snapshots through the same SSRF-guarded HTTP client used by WebFetch.  A
Playwright backend can be selected explicitly when the optional package and
browser runtime are installed; it never exposes arbitrary JavaScript, cookies,
storage, headers, or file URLs to the model.
"""

from .runtime import (
    BrowserBackend,
    BrowserBackendError,
    BrowserElement,
    BrowserOptions,
    BrowserPage,
    BrowserRuntime,
    BrowserSessionError,
)

__all__ = [
    "BrowserBackend",
    "BrowserBackendError",
    "BrowserElement",
    "BrowserOptions",
    "BrowserPage",
    "BrowserRuntime",
    "BrowserSessionError",
]
