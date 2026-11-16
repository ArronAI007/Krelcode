"""Layered agent rules, on-demand directory rules and safe ``@import`` expansion.

Rule precedence is intentionally narrow-to-broad at render time::

    user (~/.krelcode/AGENTS.md) -> project (<workspace>/AGENTS.md)
        -> directory (<workspace>/subdir/AGENTS.md)

Only ``AGENTS.md`` or ``KRELCODE.md`` is selected at each directory, mirroring
the existing project-rule compatibility contract.  Directory rule files are
discovered from paths mentioned by the current turn, not by recursively walking
the repository.  This keeps unrelated subtrees out of the prompt and avoids a
token-cost regression for large monorepos.

Imports use a standalone Markdown line ``@relative/path``.  Import directives
inside fenced code blocks and inline mentions (for example email addresses) are
left untouched.  Relative imports resolve against the importing file.  User
rules may only import below ``~/.krelcode``; project and directory rules may
only import below the workspace root.  Sensitive files, cycles, duplicate
imports and oversized trees are rejected or bounded and reported as structured
diagnostics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence, Set, Tuple

from ..core.messages import Message
from ..tools.paths import read_text_file
from ..tools.secret_guard import classify_secret_path

RULE_FILE_NAMES = ("AGENTS.md", "KRELCODE.md")
USER_RULE_RELPATH = Path(".krelcode") / "AGENTS.md"
DEFAULT_RULES_MAX_CHARS = 8_000
DEFAULT_IMPORT_MAX_DEPTH = 5
DEFAULT_IMPORT_MAX_FILES = 32
MAX_RULE_FILE_BYTES = 256_000
TRUNCATION_MARKER = "\n…[规则内容已按字符预算截断]"
_USER_IMPORT_BLOCKED_FILES = frozenset({"config.toml", "allow_rules.json"})
_USER_IMPORT_BLOCKED_DIRS = frozenset({"memory", "sandbox", "sessions"})

_IMPORT_RE = re.compile(r"^\s*@(.+?)\s*$")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_ATTACHED_FILE_RE = re.compile(r'<attached_file path="([^"]+)">')
_PATH_MENTION_RE = re.compile(
    r"(?<![\w/.-])("
    r"(?:\.{0,2}/)?(?:[\w@.+-]+/)+[\w@.+-]+/?"
    r"|[\w@.+-]+\.(?:py|pyi|js|jsx|ts|tsx|java|go|rs|c|cc|cpp|h|hpp|"
    r"rb|php|swift|kt|kts|scala|sh|bash|zsh|fish|sql|json|toml|yaml|yml|"
    r"md|mdx|txt|cfg|ini|html|css|scss|vue|svelte|xml|lua|r|m|mm|cs|ex|"
    r"exs|dart|ipynb)"
    r")(?![\w/.-])",
    re.IGNORECASE,
)
_PATH_ARG_KEYS = frozenset({
    "path", "file", "file_path", "filename", "directory", "dir",
    "cwd", "workdir", "working_directory",
})


class RuleScope(str, Enum):
    """Rule ownership, ordered from broad to specific."""

    USER = "user"
    PROJECT = "project"
    DIRECTORY = "directory"


@dataclass(frozen=True)
class RuleLimits:
    """Hard bounds applied to one layered-rule load."""

    max_chars: int = DEFAULT_RULES_MAX_CHARS
    import_max_depth: int = DEFAULT_IMPORT_MAX_DEPTH
    import_max_files: int = DEFAULT_IMPORT_MAX_FILES

    def normalized(self) -> "RuleLimits":
        return RuleLimits(
            max_chars=max(256, min(int(self.max_chars), 100_000)),
            import_max_depth=max(0, min(int(self.import_max_depth), 12)),
            import_max_files=max(0, min(int(self.import_max_files), 128)),
        )


@dataclass(frozen=True)
class RuleSource:
    """One loaded entry file or imported fragment."""

    path: Path
    scope: RuleScope
    display_path: str
    chars: int
    depth: int = 0
    imported_by: Optional[Path] = None

    @property
    def imported(self) -> bool:
        return self.imported_by is not None


@dataclass(frozen=True)
class RuleIssue:
    """A non-fatal import/discovery problem; messages never contain file data."""

    code: str
    path: str
    message: str


@dataclass(frozen=True)
class RuleBundle:
    """Rendered rules plus source and import diagnostics."""

    content: str
    sources: Tuple[RuleSource, ...] = ()
    issues: Tuple[RuleIssue, ...] = ()
    targets: Tuple[Path, ...] = ()
    truncated: bool = False
    import_count: int = 0

    @property
    def chars(self) -> int:
        return len(self.content)

    @property
    def entry_count(self) -> int:
        return sum(1 for source in self.sources if not source.imported)

    def summary_lines(self) -> List[str]:
        if not self.content:
            return ["规则: 未发现"]
        lines = [
            f"规则: {self.entry_count} 个入口 · {self.import_count} 个导入"
            f" · {self.chars} chars"
            + (" · 已截断" if self.truncated else ""),
        ]
        for source in self.sources:
            marker = f" ← {self._display(source.imported_by)}" if source.imported_by else ""
            lines.append(
                f"{source.scope.value}: {source.display_path}{marker}"
                f" · {source.chars} chars")
        if self.issues:
            lines.append(f"规则问题: {len(self.issues)} 个")
        return lines

    @staticmethod
    def _display(path: Optional[Path]) -> str:
        return path.name if path is not None else "?"


@dataclass
class _LoadState:
    workspace_root: Path
    home: Path
    limits: RuleLimits
    imports_enabled: bool = True
    sources: List[RuleSource] = field(default_factory=list)
    issues: List[RuleIssue] = field(default_factory=list)
    seen: Set[Path] = field(default_factory=set)
    import_count: int = 0
    truncated: bool = False
    parts: List[str] = field(default_factory=list)


class RuleLoader:
    """Discover layered rule files and safely expand their imports."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        home: Optional[Path] = None,
        limits: Optional[RuleLimits] = None,
        imports_enabled: bool = True,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.home = (Path.home() if home is None else Path(home)).resolve()
        self.limits = (limits or RuleLimits()).normalized()
        self.imports_enabled = imports_enabled

    def load(self, target_paths: Iterable[Path] = ()) -> RuleBundle:
        """Load user/project rules plus directory rules for ``target_paths``."""
        targets = normalize_rule_targets(self.workspace_root, target_paths)
        state = self._state()
        self._load_user(state)
        self._load_project(state)
        self._load_directories(state, targets)
        return self._finish(state, targets)

    def load_base(self) -> RuleBundle:
        """Load only user and project entry layers (stable session prefix)."""
        state = self._state()
        self._load_user(state)
        self._load_project(state)
        return self._finish(state, ())

    def load_user(self) -> RuleBundle:
        """Load only the managed-path user instruction layer."""
        state = self._state()
        self._load_user(state)
        return self._finish(state, ())

    def load_project(self) -> RuleBundle:
        """Load only the project-root instruction layer."""
        state = self._state()
        self._load_project(state)
        return self._finish(state, ())

    def load_directory(self, target_paths: Iterable[Path]) -> RuleBundle:
        """Load only on-demand directory layers for the supplied targets."""
        targets = normalize_rule_targets(self.workspace_root, target_paths)
        state = self._state()
        self._load_directories(state, targets)
        return self._finish(state, targets)

    def _state(self) -> _LoadState:
        return _LoadState(
            workspace_root=self.workspace_root,
            home=self.home,
            limits=self.limits,
            imports_enabled=self.imports_enabled,
        )

    def _load_user(self, state: _LoadState) -> None:
        path = self.home / USER_RULE_RELPATH
        self._load_entry(state, path, RuleScope.USER, trusted_root=self.home / ".krelcode")

    def _load_project(self, state: _LoadState) -> None:
        path = _first_rule_file(self.workspace_root)
        if path is not None:
            self._load_entry(
                state, path, RuleScope.PROJECT, trusted_root=self.workspace_root)

    def _load_directories(
        self, state: _LoadState, targets: Sequence[Path],
    ) -> None:
        for path in discover_directory_rule_files(self.workspace_root, targets):
            self._load_entry(
                state, path, RuleScope.DIRECTORY, trusted_root=self.workspace_root)

    def _load_entry(
        self,
        state: _LoadState,
        path: Path,
        scope: RuleScope,
        *,
        trusted_root: Path,
    ) -> None:
        resolved = _safe_resolve(path, trusted_root)
        if resolved is None:
            return
        if resolved in state.seen:
            return
        text = self._read_text(state, resolved, required=False)
        if text is None:
            return
        state.seen.add(resolved)
        state.sources.append(RuleSource(
            path=resolved,
            scope=scope,
            display_path=self._display_path(resolved, scope),
            chars=len(text),
        ))
        expanded = self._expand(state, text, resolved, scope, trusted_root, 0)
        heading = _heading(scope, self._display_path(resolved, scope))
        state.parts.append(f"{heading}:\n{expanded}")

    def _expand(
        self,
        state: _LoadState,
        text: str,
        source_path: Path,
        scope: RuleScope,
        trusted_root: Path,
        depth: int,
    ) -> str:
        if not state.imports_enabled:
            return text
        out: List[str] = []
        fence: Optional[str] = None
        for line in text.splitlines(keepends=True):
            fence_match = _FENCE_RE.match(line)
            if fence_match:
                marker = fence_match.group(1)[0]
                if fence is None:
                    fence = marker
                elif fence == marker:
                    fence = None
                out.append(line)
                continue
            match = _IMPORT_RE.match(line) if fence is None else None
            if match is None:
                out.append(line)
                continue
            raw_target = _clean_import_target(match.group(1))
            if not raw_target:
                out.append(line)
                continue
            imported = self._resolve_import(
                state, raw_target, source_path, scope, trusted_root)
            if imported is None:
                continue
            if imported in state.seen:
                self._issue(
                    state, "duplicate-import", imported,
                    "规则文件已被导入，已跳过重复内容")
                continue
            if depth >= state.limits.import_max_depth:
                self._issue(
                    state, "import-depth", imported,
                    f"导入深度超过上限 {state.limits.import_max_depth}")
                continue
            if state.import_count >= state.limits.import_max_files:
                self._issue(
                    state, "import-limit", imported,
                    f"导入文件数超过上限 {state.limits.import_max_files}")
                continue
            imported_text = self._read_text(state, imported, required=True)
            if imported_text is None:
                continue
            state.seen.add(imported)
            state.import_count += 1
            state.sources.append(RuleSource(
                path=imported,
                scope=scope,
                display_path=self._display_path(imported, scope),
                chars=len(imported_text),
                depth=depth + 1,
                imported_by=source_path,
            ))
            nested = self._expand(
                state, imported_text, imported, scope, trusted_root, depth + 1)
            label = self._display_path(imported, scope)
            out.append(
                f'\n<imported_rule source="{label}">\n'
                f"{nested.rstrip()}\n</imported_rule>\n")
        return "".join(out)

    def _resolve_import(
        self,
        state: _LoadState,
        raw: str,
        source_path: Path,
        scope: RuleScope,
        trusted_root: Path,
    ) -> Optional[Path]:
        if "://" in raw:
            self._issue(
                state, "import-url", source_path,
                "不支持 URL 导入；请使用作用域内相对路径")
            return None
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = source_path.parent / candidate
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            self._issue(
                state, "import-missing", candidate,
                "导入目标不存在或无法解析")
            return None
        if resolved != trusted_root and trusted_root not in resolved.parents:
            self._issue(
                state, "import-outside-scope", resolved,
                "导入目标越出当前规则作用域，已拒绝")
            return None
        sensitive = classify_secret_path(resolved, trusted_root)
        if sensitive is not None or _blocked_user_runtime_file(
            resolved, trusted_root, scope):
            self._issue(
                state, "import-sensitive", resolved,
                "导入目标命中敏感文件保护，已拒绝")
            return None
        if not resolved.is_file():
            self._issue(
                state, "import-not-file", resolved,
                "导入目标不是普通文件")
            return None
        return resolved

    def _read_text(
        self, state: _LoadState, path: Path, *, required: bool,
    ) -> Optional[str]:
        try:
            if path.stat().st_size > MAX_RULE_FILE_BYTES:
                self._issue(
                    state, "rule-too-large", path,
                    f"规则文件超过 {MAX_RULE_FILE_BYTES} bytes，已跳过")
                return None
            text = read_text_file(path)
            return text[1:] if text.startswith("\ufeff") else text
        except FileNotFoundError:
            if required:
                self._issue(state, "import-missing", path, "导入目标不存在")
            return None
        except Exception as exc:
            self._issue(
                state, "rule-read-failed", path,
                f"规则文件读取失败: {type(exc).__name__}")
            return None

    def _finish(self, state: _LoadState, targets: Sequence[Path]) -> RuleBundle:
        rendered = "\n\n".join(state.parts)
        if len(rendered) > state.limits.max_chars:
            reserve = len(TRUNCATION_MARKER)
            keep = max(0, state.limits.max_chars - reserve)
            rendered = rendered[:keep].rstrip() + TRUNCATION_MARKER
            state.truncated = True
        return RuleBundle(
            content=rendered,
            sources=tuple(state.sources),
            issues=tuple(state.issues),
            targets=tuple(targets),
            truncated=state.truncated,
            import_count=state.import_count,
        )

    def _display_path(self, path: Path, scope: RuleScope) -> str:
        if scope is RuleScope.USER:
            try:
                return "~/.krelcode/" + path.relative_to(
                    self.home / ".krelcode").as_posix()
            except ValueError:
                return str(path)
        try:
            return path.relative_to(self.workspace_root).as_posix()
        except ValueError:
            return str(path)

    @staticmethod
    def _issue(
        state: _LoadState, code: str, path: Path, message: str,
    ) -> None:
        state.issues.append(RuleIssue(code=code, path=str(path), message=message))



def _blocked_user_runtime_file(
    path: Path, trusted_root: Path, scope: RuleScope,
) -> bool:
    if scope is not RuleScope.USER:
        return False
    try:
        rel = path.relative_to(trusted_root)
    except ValueError:
        return True
    if rel.name in _USER_IMPORT_BLOCKED_FILES:
        return True
    return bool(rel.parts and rel.parts[0] in _USER_IMPORT_BLOCKED_DIRS)


def _heading(scope: RuleScope, display_path: str) -> str:
    if scope is RuleScope.USER:
        return f"全局规则（来自 {display_path}，务必遵守；优先级较低）"
    if scope is RuleScope.PROJECT:
        return f"项目规则（来自 {display_path}，务必遵守；覆盖全局规则）"
    return f"目录规则（来自 {display_path}，务必遵守；覆盖上层规则）"


def _clean_import_target(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'`":
        value = value[1:-1].strip()
    elif value.startswith("<") and value.endswith(">"):
        value = value[1:-1].strip()
    return value


def _safe_resolve(path: Path, trusted_root: Path) -> Optional[Path]:
    try:
        root = trusted_root.resolve()
        resolved = Path(path).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    if resolved != root and root not in resolved.parents:
        return None
    if not resolved.is_file():
        return None
    return resolved


def _first_rule_file(directory: Path) -> Optional[Path]:
    for name in RULE_FILE_NAMES:
        candidate = directory / name
        if candidate.exists():
            resolved = _safe_resolve(candidate, directory)
            if resolved is not None:
                return resolved
    return None


def discover_directory_rule_files(
    workspace_root: Path,
    target_paths: Iterable[Path],
) -> Tuple[Path, ...]:
    """Find one rule file in each ancestor directory of each in-root target."""
    root = Path(workspace_root).resolve()
    discovered: Set[Path] = set()
    for target in normalize_rule_targets(root, target_paths):
        raw = target if target.exists() else target.parent
        while raw != root and not raw.exists():
            raw = raw.parent
        try:
            directory = raw.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            continue
        if directory != root and root not in directory.parents:
            continue
        if directory.is_file():
            directory = directory.parent
        while directory != root and (directory == root or root in directory.parents):
            rule_file = _first_rule_file(directory)
            if rule_file is not None:
                discovered.add(rule_file)
            directory = directory.parent
    return tuple(sorted(discovered, key=lambda path: (
        len(path.relative_to(root).parts), path.as_posix())))


def normalize_rule_targets(
    workspace_root: Path,
    target_paths: Iterable[Path],
) -> Tuple[Path, ...]:
    root = Path(workspace_root).resolve()
    targets: List[Path] = []
    for raw in target_paths:
        try:
            path = Path(raw).expanduser()
            if not path.is_absolute():
                path = root / path
            resolved = path.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            continue
        if resolved != root and root not in resolved.parents:
            continue
        if resolved not in targets:
            targets.append(resolved)
    return tuple(targets)


def extract_rule_targets(
    workspace_root: Path,
    *,
    intent: Any = None,
    history: Sequence[Message] = (),
) -> Tuple[Path, ...]:
    """Extract files/directories relevant to the current turn for rule discovery."""
    raw_targets: List[Path] = []
    for path in getattr(intent, "target_files", ()) or ():
        raw_targets.append(Path(path))

    current = _current_turn_messages(history)
    for message in current:
        content = message.content or ""
        if message.role == "user":
            for match in _ATTACHED_FILE_RE.findall(content):
                raw_targets.append(Path(match))
            for match in _PATH_MENTION_RE.findall(content):
                if "://" not in match:
                    raw_targets.append(Path(match.rstrip(".,，。;；:：")))
        for call in message.tool_calls or ():
            _collect_argument_paths(call.arguments, raw_targets)
    return normalize_rule_targets(workspace_root, raw_targets)


def _current_turn_messages(history: Sequence[Message]) -> Tuple[Message, ...]:
    if not history:
        return ()
    start = 0
    for index in range(len(history) - 1, -1, -1):
        message = history[index]
        if message.role == "assistant" and not message.tool_calls:
            start = index + 1
            break
    return tuple(history[start:])


def _collect_argument_paths(value: Any, out: List[Path]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in _PATH_ARG_KEYS:
                if isinstance(item, str) and item.strip():
                    out.append(Path(item))
                elif isinstance(item, (list, tuple)):
                    out.extend(Path(raw) for raw in item if isinstance(raw, str) and raw.strip())
            elif isinstance(item, (dict, list, tuple)):
                _collect_argument_paths(item, out)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_argument_paths(item, out)


# Backwards-friendly convenience API.
def load_rules(
    workspace_root: Path,
    *,
    home: Optional[Path] = None,
    target_paths: Iterable[Path] = (),
    limits: Optional[RuleLimits] = None,
    imports_enabled: bool = True,
) -> RuleBundle:
    return RuleLoader(
        workspace_root,
        home=home,
        limits=limits,
        imports_enabled=imports_enabled,
    ).load(target_paths)
