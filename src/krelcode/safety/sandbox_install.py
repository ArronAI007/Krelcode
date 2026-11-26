"""Resolve user-owned installation roots from an installation command."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Sequence
from urllib.parse import ParseResult, urlparse

from .commands import _tokenize, _tokenize_cmd
from .sandbox_audit import _inside

_INSTALL_NAME = re.compile(
    r"(?:^|[._-])(?P<app>[a-z0-9][a-z0-9._-]{1,50}?)"
    r"(?:[._-]install(?:er)?)(?:[._-](?:sh|bash|zsh|py|command))?$",
    re.IGNORECASE,
)
_APP_ALIASES = {
    "hermes-agent": "hermes",
    "hermes-cli": "hermes",
}
_BLOCKED_APP_DIRS = {
    ".ssh", ".aws", ".azure", ".kube", ".gnupg", ".docker",
    ".password-store", ".config", ".codex", ".krelcode",
}
_GENERIC_URL_SEGMENTS = {
    "main", "master", "latest", "raw", "refs", "heads", "blob", "download",
}


def inferred_user_install_roots(
    command: str,
    user_home: Path,
    existing_roots: Sequence[Path],
    *,
    shell_dialect: str = "posix",
) -> tuple[Path, ...]:
    """Infer app-home roots such as ``~/.hermes`` from installer names/URLs.

    This remains bounded to a direct, non-sensitive child of the real home and
    is only consulted for commands already classified as user installations.
    """
    windows_paths = shell_dialect == "cmd"
    try:
        tokens = (
            _tokenize_cmd(command)
            if windows_paths
            else _tokenize(command)
        )
    except ValueError:
        return ()
    home = Path(user_home).expanduser().resolve()
    known = {Path(root).resolve() for root in existing_roots}
    inferred: list[Path] = []
    for app in _install_app_names(tokens):
        normalized = _APP_ALIASES.get(app, app).strip("._-")
        if not normalized or normalized in _GENERIC_URL_SEGMENTS:
            continue
        directory = f".{normalized}"
        if directory in _BLOCKED_APP_DIRS:
            continue
        root = (home / directory).resolve()
        if root in known or not _inside(root, home) or root == home:
            continue
        known.add(root)
        inferred.append(root)
    return tuple(inferred)


def _install_app_names(tokens: Sequence[str]) -> tuple[str, ...]:
    names: list[str] = []
    for token in tokens:
        parsed = urlparse(token)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            names.extend(_app_names_from_url(parsed))
            continue
        basename = Path(token.replace("\\", "/")).name
        match = _INSTALL_NAME.search(basename)
        if match:
            names.append(match.group("app").casefold())
    return tuple(dict.fromkeys(names))


def _app_names_from_url(parsed: ParseResult) -> tuple[str, ...]:
    path = parsed.path
    hostname = parsed.hostname or ""
    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        return ()
    leaf = segments[-1].casefold()
    if "install" not in leaf and not leaf.endswith((".sh", ".command")):
        return ()
    if hostname.endswith("github.com") and len(segments) >= 2:
        candidate = segments[1].removesuffix(".git").casefold()
        return (candidate,)
    if hostname == "raw.githubusercontent.com" and len(segments) >= 3:
        candidate = segments[1].removesuffix(".git").casefold()
        return (candidate,)
    for segment in reversed(segments[:-1]):
        candidate = segment.casefold()
        if candidate not in _GENERIC_URL_SEGMENTS:
            return (candidate,)
    return ()


__all__ = ["inferred_user_install_roots"]
