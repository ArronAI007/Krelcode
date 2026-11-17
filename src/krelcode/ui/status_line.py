"""Safe, deterministic status-line templating for the REPL.

Status lines are data, not shell commands. The renderer accepts only an explicit
set of placeholders, strips control characters, and never reads disk or spawns
processes during a redraw. This keeps a 150 ms refresh loop predictable and
prevents project configuration from turning the prompt into a command runner.
"""

from __future__ import annotations

import os
import re
import string
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

DEFAULT_TEMPLATE = "{status} · {permission} · Shift-Tab 切换 · Ctrl-O 面板"
ALLOWED_FIELDS = frozenset({
    "provider", "model", "permission", "session", "cwd",
    "status", "tokens", "context", "branch",
})
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_EMPTY_SEPARATOR = re.compile(r"(?:^|\s·\s)(?:\s·\s)+|^ · | · $")


@dataclass(frozen=True)
class StatusLineOptions:
    enabled: bool = True
    template: str = DEFAULT_TEMPLATE
    separator: str = " · "
    max_length: int = 0

    @classmethod
    def from_config(cls, section: Any) -> "StatusLineOptions":
        data = section if isinstance(section, Mapping) else {}
        template = str(data.get("template") or DEFAULT_TEMPLATE)
        separator = str(data.get("separator") or " · ")
        try:
            max_length = int(data.get("max_length") or 0)
        except (TypeError, ValueError):
            max_length = 0
        return cls(
            enabled=bool(data.get("enabled", True)),
            template=template,
            separator=separator,
            max_length=max(0, max_length),
        )


class StatusLineRenderer:
    """Render one status line from a fixed set of runtime values."""

    def __init__(self, options: Optional[StatusLineOptions] = None) -> None:
        self.options = options or StatusLineOptions()
        self._formatter = string.Formatter()
        try:
            fields = [
                field_name
                for _, field_name, _, _ in self._formatter.parse(
                    self.options.template)
                if field_name
            ]
        except ValueError as exc:
            raise ValueError(f"status_line.template 非法: {exc}") from exc
        unknown = [field for field in fields if field not in ALLOWED_FIELDS]
        if unknown:
            raise ValueError(
                "status_line.template 包含未知变量: "
                + ", ".join(sorted(set(unknown))))
        self.fields = tuple(fields)

    def render(
        self,
        *,
        provider: str = "",
        model: str = "",
        permission: str = "",
        session: str = "",
        cwd: str = "",
        status: str = "",
        tokens: str | int = "",
        context: str = "",
        branch: str = "",
    ) -> str:
        if not self.options.enabled:
            return ""
        values = {
            "provider": _clean(provider),
            "model": _clean(model),
            "permission": _clean(permission),
            "session": _clean(session),
            "cwd": _clean(cwd),
            "status": _clean(status),
            "tokens": _clean(tokens),
            "context": _clean(context),
            "branch": _clean(branch),
        }
        try:
            rendered = self.options.template.format_map(values)
        except (KeyError, ValueError, AttributeError):
            # A malformed user template should degrade to a useful status line,
            # never take down the REPL.
            rendered = DEFAULT_TEMPLATE.format_map(values)
        rendered = _clean(rendered)
        rendered = _normalize_separators(rendered, self.options.separator)
        if self.options.max_length:
            rendered = _truncate(rendered, self.options.max_length)
        return rendered


def read_git_branch(workspace_root: str | Path) -> str:
    """Read the current branch once for a session; never called on redraw."""
    cwd = Path(workspace_root).expanduser()
    if not cwd.is_dir():
        return ""
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
            env=_safe_git_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    branch = result.stdout.strip().splitlines()
    return branch[0] if branch else ""


def _safe_git_env() -> dict[str, str]:
    # Git branch lookup needs PATH/HOME and local git configuration, but not
    # the coding agent's model credentials.
    result: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if any(marker in upper for marker in (
                "API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")):
            continue
        result[key] = value
    return result


def _clean(value: Any) -> str:
    text = str(value or "")
    text = _CONTROL.sub("", text)
    return " ".join(text.split())


def _normalize_separators(text: str, separator: str) -> str:
    if separator != " · ":
        return text.strip()
    text = re.sub(r"(?:\s*·\s*){2,}", " · ", text)
    text = re.sub(r"^[·\s]+", "", text)
    text = re.sub(r"[·\s]+$", "", text)
    return text.strip()


def _truncate(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[:width - 1] + "…"


__all__ = [
    "ALLOWED_FIELDS",
    "DEFAULT_TEMPLATE",
    "StatusLineOptions",
    "StatusLineRenderer",
    "read_git_branch",
]
