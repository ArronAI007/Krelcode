"""Workspace/user Markdown slash commands (``CommandSource.CUSTOM``)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional, Tuple

from ...skills.parser import (
    SkillParseError,
    parse_skill_document,
    split_allowed_tools,
)

MAX_COMMAND_BYTES = 256_000
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class CustomCommand:
    name: str
    description: str
    prompt: str
    path: Path
    source: str
    argument_hint: str = ""
    allowed_tools: Tuple[str, ...] = ()

    def render(self, arguments: str = "") -> str:
        raw = arguments.strip()
        has_placeholder = (
            "$ARGUMENTS" in self.prompt or "${ARGUMENTS}" in self.prompt)
        text = self.prompt.replace("${ARGUMENTS}", raw).replace(
            "$ARGUMENTS", raw)
        if raw and not has_placeholder:
            text = text.rstrip() + "\n\n## User arguments\n\n" + raw
        return (
            f'<custom_command name="{self.name}" source="{self.source}">\n'
            f"{text.rstrip()}\n</custom_command>"
        )


@dataclass(frozen=True)
class CustomCommandIssue:
    path: Path
    message: str


@dataclass(frozen=True)
class _Root:
    path: Path
    source: str


class CustomCommandManager:
    """Discover Markdown commands from user, project and configured roots."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        home: Optional[Path] = None,
        extra_paths: Iterable[str | Path] = (),
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.home = (
            Path(home).expanduser() if home is not None else Path.home()
        ).expanduser().resolve()
        self.extra_paths = tuple(
            Path(path).expanduser() for path in extra_paths if str(path).strip())
        self._commands: dict[str, CustomCommand] = {}
        self._issues: tuple[CustomCommandIssue, ...] = ()
        self.reload()

    def _roots(self) -> tuple[_Root, ...]:
        candidates = [
            _Root(self.home / ".krelcode" / "commands", "user"),
            _Root(self.workspace_root / ".krelcode" / "commands", "project"),
            _Root(self.workspace_root / ".commands", "project"),
        ]
        for raw in self.extra_paths:
            path = raw if raw.is_absolute() else self.workspace_root / raw
            candidates.append(_Root(path.resolve(), "custom"))
        result: list[_Root] = []
        seen: set[Path] = set()
        for candidate in candidates:
            resolved = candidate.path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            result.append(_Root(resolved, candidate.source))
        return tuple(result)

    def reload(self) -> None:
        commands: dict[str, CustomCommand] = {}
        issues: list[CustomCommandIssue] = []
        for root in self._roots():
            if not root.path.is_dir():
                continue
            try:
                paths = sorted(root.path.glob("*.md"), key=lambda path: path.name)
            except OSError as exc:
                issues.append(CustomCommandIssue(root.path, f"无法扫描目录: {exc}"))
                continue
            for path in paths:
                try:
                    command = self._load(path, root)
                except (OSError, ValueError, SkillParseError) as exc:
                    issues.append(CustomCommandIssue(path, str(exc)))
                    continue
                commands[command.name.lower()] = command
        self._commands = commands
        self._issues = tuple(issues)

    def _load(self, path: Path, root: _Root) -> CustomCommand:
        resolved = path.resolve()
        if not resolved.is_relative_to(root.path):
            raise ValueError("Custom Command 越出命令目录")
        size = resolved.stat().st_size
        if size > MAX_COMMAND_BYTES:
            raise ValueError(
                f"Custom Command 过大（{size} > {MAX_COMMAND_BYTES} bytes）")
        metadata, body = parse_skill_document(
            resolved.read_text(encoding="utf-8"), fallback_name=resolved.stem)
        name = str(metadata.get("name") or resolved.stem).strip()
        if not _NAME_RE.fullmatch(name):
            raise ValueError(f"非法 Custom Command 名称: {name!r}")
        prompt = body.strip()
        if not prompt:
            raise ValueError("Custom Command 正文不能为空")
        description = " ".join(str(metadata.get("description") or "").split())
        if not description:
            description = next(
                (line.strip("# ").strip() for line in prompt.splitlines()
                 if line.strip()), f"Custom command {name}")[:320]
        return CustomCommand(
            name=name,
            description=description,
            prompt=prompt,
            path=resolved,
            source=root.source,
            argument_hint=str(metadata.get("argument-hint") or ""),
            allowed_tools=split_allowed_tools(metadata.get("allowed-tools")),
        )

    def list(self) -> tuple[CustomCommand, ...]:
        return tuple(sorted(
            self._commands.values(), key=lambda command: command.name.lower()))

    def get(self, name: str) -> Optional[CustomCommand]:
        return self._commands.get(str(name).strip().lstrip("/").lower())

    @property
    def issues(self) -> tuple[CustomCommandIssue, ...]:
        return self._issues
