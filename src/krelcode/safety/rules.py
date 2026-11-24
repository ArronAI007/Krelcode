"""审批规则：统一 allow / ask / deny，并兼容旧版 allow 规则文件。

规则有两种作用域：
- ``session``：只存在于当前进程，不落盘；
- ``workspace``：持久化到 ``<workspace>/.krelcode/allow_rules.json``。

匹配对象包括精确命令、命令程序名、工作区相对路径 glob 和工具名。决策优先级
固定为 ``deny > ask > allow``，因此新增的 deny 不会被旧白名单绕过。旧格式
``allow_programs`` / ``allow_commands`` 继续读取和写入；只有存在新版 ``rules``
时才添加该字段，避免无意义地改动已有规则文件。
"""

from __future__ import annotations

import fnmatch
import json
import re
import shlex
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Set, Tuple

from .commands import REDIRECTS, SEPARATORS, _tokenize

_WRAPPER_PROGRAMS = {
    "env", "command", "exec", "nohup", "nice", "time", "timeout", "xargs",
}
_PROBABLE_SECRET = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|auth(?:orization)?|password|"
    r"client[_-]?secret|private[_-]?key|secret)[\"']?\s*[:=]\s*\S+"
)


class RuleDecision(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class RuleScope(str, Enum):
    SESSION = "session"
    WORKSPACE = "workspace"


class RuleKind(str, Enum):
    COMMAND = "command"
    COMMAND_PROGRAM = "command_program"
    PATH = "path"
    TOOL = "tool"


@dataclass(frozen=True)
class PermissionRule:
    id: str
    decision: RuleDecision
    scope: RuleScope
    kind: RuleKind
    pattern: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision", RuleDecision(self.decision))
        object.__setattr__(self, "scope", RuleScope(self.scope))
        object.__setattr__(self, "kind", RuleKind(self.kind))
        object.__setattr__(self, "pattern", str(self.pattern).strip())


@dataclass(frozen=True)
class RuleMatch:
    """一次规则匹配结果；reason 可直接用于审批/拒绝说明。"""

    rule: PermissionRule
    legacy: bool = False

    @property
    def decision(self) -> RuleDecision:
        return self.rule.decision

    @property
    def reason(self) -> str:
        source = (
            "旧版 allow 规则"
            if self.legacy
            else f"{self.rule.scope.value} {self.rule.id}"
        )
        return (
            f"命中 {source}（{self.rule.decision.value}）: "
            f"{self.rule.kind.value} {self.rule.pattern}")


def _basename(program: str) -> str:
    return program.rsplit("/", 1)[-1] if "/" in program else program


def command_program(command: str) -> Optional[str]:
    """提取命令首个程序名（basename）；解析失败返回 None。"""
    try:
        tokens = _tokenize(command)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return None
    return _basename(tokens[0])


def command_key(command: str) -> Optional[str]:
    """返回用于精确授权的规范化命令；无法解析时返回 None。"""
    try:
        tokens = _tokenize(command)
    except ValueError:
        return None
    if not tokens or _PROBABLE_SECRET.search(command):
        return None
    return shlex.join(tokens)


def _simple_program(command: str) -> Optional[str]:
    """仅对无复合结构、无包装的直接命令返回程序名。"""
    try:
        tokens = _tokenize(command)
    except ValueError:
        return None
    if not tokens or any(
            token in SEPARATORS or token in REDIRECTS for token in tokens):
        return None
    first = tokens[0]
    if "=" in first or _basename(first).lower() in _WRAPPER_PROGRAMS:
        return None
    return _basename(first)


def normalize_rule_path(pattern: str) -> str:
    """校验并规范化工作区相对 glob；拒绝绝对路径和 ``..`` 穿越。"""
    raw = str(pattern or "").strip().replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]
    if not raw:
        raise ValueError("路径规则不能为空")
    path = Path(raw)
    if path.is_absolute():
        raise ValueError("路径规则必须是相对工作区的 glob")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise ValueError("路径规则不能包含 ..")
    return "/".join(parts)


def workspace_relative_path(raw_path: str, workspace_root: Path) -> Optional[str]:
    """返回真实解析后的工作区相对路径；越界或解析失败返回 None。"""
    if not str(raw_path).strip():
        return None
    root = Path(workspace_root).resolve()
    candidate = Path(str(raw_path)).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=False)
        return resolved.relative_to(root).as_posix()
    except (ValueError, OSError):
        return None


class AllowRules:
    """兼容旧白名单的规则仓库；新规则由显式用户命令管理。"""

    def __init__(
        self,
        path: Optional[Path] = None,
        *,
        workspace_root: Optional[Path] = None,
    ) -> None:
        self.path = path
        self.workspace_root = (
            Path(workspace_root)
            if workspace_root is not None
            else (path.parent.parent if path is not None and path.parent.name == ".krelcode"
                  else (path.parent if path is not None else Path.cwd()))
        )
        self.programs: Set[str] = set()
        self.commands: Set[str] = set()
        self.session_rules: List[PermissionRule] = []
        self.workspace_rules: List[PermissionRule] = []
        if path is not None and path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.programs = {
                    str(item) for item in data.get("allow_programs", [])
                }
                self.commands = {
                    str(item) for item in data.get("allow_commands", [])
                }
                self.workspace_rules = self._parse_rules(
                    data.get("rules", []), RuleScope.WORKSPACE)
            except (OSError, json.JSONDecodeError, TypeError):
                # 规则文件损坏 → 清空新版规则，旧白名单也保持 fail-closed。
                self.programs = set()
                self.commands = set()
                self.workspace_rules = []

    @staticmethod
    def _parse_rules(
        raw_rules: object, scope: RuleScope,
    ) -> List[PermissionRule]:
        if not isinstance(raw_rules, list):
            return []
        parsed: List[PermissionRule] = []
        for raw in raw_rules:
            if not isinstance(raw, dict):
                continue
            try:
                decision = RuleDecision(str(raw.get("decision", "")))
                rule_scope = RuleScope(str(raw.get("scope", scope.value)))
                kind = RuleKind(str(raw.get("kind", "")))
                pattern = str(raw.get("pattern", "")).strip()
                rule_id = str(raw.get("id", "")).strip()
                if rule_scope is not scope or not pattern or not rule_id:
                    continue
                if kind is RuleKind.PATH:
                    pattern = normalize_rule_path(pattern)
                elif kind is RuleKind.COMMAND_PROGRAM:
                    pattern = _basename(pattern)
                elif kind is RuleKind.COMMAND:
                    pattern = command_key(pattern) or ""
                    if not pattern:
                        continue
                parsed.append(PermissionRule(
                    rule_id, decision, rule_scope, kind, pattern))
            except (ValueError, TypeError):
                continue
        return parsed

    @property
    def rules(self) -> Tuple[PermissionRule, ...]:
        return tuple(self.session_rules + self.workspace_rules)

    def matches(self, command: str) -> bool:
        """兼容旧接口：仅表示命令最终命中 allow。"""
        match = self.evaluate_command(command)
        return match is not None and match.decision is RuleDecision.ALLOW

    def evaluate_command(self, command: str) -> Optional[RuleMatch]:
        key = command_key(command)
        prog = _simple_program(command)
        candidates: List[RuleMatch] = []
        if key is not None and key in self.commands:
            candidates.append(RuleMatch(PermissionRule(
                "legacy-command", RuleDecision.ALLOW, RuleScope.WORKSPACE,
                RuleKind.COMMAND, key), legacy=True))
        if prog is not None and prog in self.programs:
            candidates.append(RuleMatch(PermissionRule(
                "legacy-program", RuleDecision.ALLOW, RuleScope.WORKSPACE,
                RuleKind.COMMAND_PROGRAM, prog), legacy=True))
        for rule in self.rules:
            if rule.kind is RuleKind.COMMAND:
                if key is not None and key == rule.pattern:
                    candidates.append(RuleMatch(rule))
            elif rule.kind is RuleKind.COMMAND_PROGRAM:
                if prog is not None and prog == rule.pattern:
                    candidates.append(RuleMatch(rule))
        return _highest_priority(candidates)

    def evaluate_path(self, raw_path: str) -> Optional[RuleMatch]:
        relative = workspace_relative_path(str(raw_path), self.workspace_root)
        if relative is None:
            return None
        candidates = [
            RuleMatch(rule) for rule in self.rules
            if rule.kind is RuleKind.PATH
            and fnmatch.fnmatchcase(relative, rule.pattern)
        ]
        return _highest_priority(candidates)

    def evaluate_paths(self, raw_paths: Iterable[str]) -> Optional[RuleMatch]:
        candidates = [
            match for match in (self.evaluate_path(path) for path in raw_paths)
            if match is not None
        ]
        return _highest_priority(candidates)

    def evaluate_tool(self, name: str) -> Optional[RuleMatch]:
        candidates = [
            RuleMatch(rule) for rule in self.rules
            if rule.kind is RuleKind.TOOL
            and fnmatch.fnmatchcase(name, rule.pattern)
        ]
        return _highest_priority(candidates)

    def add(self, command: str) -> bool:
        """记录精确命令并落盘；返回是否新增了规则。"""
        key = command_key(command)
        if key is None or key in self.commands:
            return False
        self.commands.add(key)
        self._save()
        return True

    def add_program(self, prog: str) -> bool:
        """直接按程序名添加旧版 allow 规则（/allow 兼容接口）。"""
        prog = _basename((prog or "").strip())
        if not prog or prog in self.programs:
            return False
        self.programs.add(prog)
        self._save()
        return True

    def remove_program(self, prog: str) -> bool:
        """移除旧版程序规则（/allow 兼容接口）。"""
        prog = _basename((prog or "").strip())
        if prog not in self.programs:
            return False
        self.programs.discard(prog)
        self._save()
        return True

    def add_rule(
        self,
        decision: RuleDecision | str,
        scope: RuleScope | str,
        kind: RuleKind | str,
        pattern: str,
    ) -> Tuple[PermissionRule, bool]:
        """添加统一规则；返回 ``(规则, 是否新增)``。"""
        decision = RuleDecision(decision)
        scope = RuleScope(scope)
        kind_value = (
            kind.value
            if isinstance(kind, RuleKind)
            else str(kind).replace("-", "_")
        )
        kind = RuleKind(kind_value)
        normalized = self._normalize_pattern(kind, pattern)
        existing = next(
            (rule for rule in self.rules
             if rule.decision is decision and rule.scope is scope
             and rule.kind is kind and rule.pattern == normalized),
            None,
        )
        if existing is not None:
            return existing, False
        scope_rules = (
            self.session_rules if scope is RuleScope.SESSION else self.workspace_rules)
        sequence = len(scope_rules) + 1
        existing_ids = {rule.id for rule in self.rules}
        prefix = f"{scope.value}-{decision.value}"
        rule_id = f"{prefix}-{sequence}"
        while rule_id in existing_ids:
            sequence += 1
            rule_id = f"{prefix}-{sequence}"
        rule = PermissionRule(rule_id, decision, scope, kind, normalized)
        scope_rules.append(rule)
        if scope is RuleScope.WORKSPACE:
            self._save()
        return rule, True

    def remove_rule(self, rule_id: str) -> bool:
        target = str(rule_id or "").strip()
        for collection, should_save in (
            (self.session_rules, False),
            (self.workspace_rules, True),
        ):
            for index, rule in enumerate(collection):
                if rule.id == target:
                    collection.pop(index)
                    if should_save:
                        self._save()
                    return True
        return False

    def _normalize_pattern(self, kind: RuleKind, pattern: str) -> str:
        raw = str(pattern or "").strip()
        if not raw:
            raise ValueError("规则 pattern 不能为空")
        if kind is RuleKind.PATH:
            return normalize_rule_path(raw)
        if kind is RuleKind.COMMAND_PROGRAM:
            return _basename(raw)
        if kind is RuleKind.COMMAND:
            key = command_key(raw)
            if key is None:
                raise ValueError("精确命令规则无法解析，或包含疑似密钥")
            return key
        return raw

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload: dict[str, object] = {
                "allow_programs": sorted(self.programs),
                "allow_commands": sorted(self.commands),
            }
            if self.workspace_rules:
                payload["rules"] = [
                    {
                        "id": rule.id,
                        "decision": rule.decision.value,
                        "scope": rule.scope.value,
                        "kind": rule.kind.value,
                        "pattern": rule.pattern,
                    }
                    for rule in self.workspace_rules
                ]
            self.path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass  # 持久化失败不阻断审批（仅本次会话有效）

    def as_list(self) -> List[str]:
        return sorted(self.programs)

    def as_commands(self) -> List[str]:
        return sorted(self.commands)

    def as_rules(self) -> List[PermissionRule]:
        return list(self.rules)

    @staticmethod
    def format_rule(rule: PermissionRule) -> str:
        return (
            f"{rule.id}: {rule.decision.value} {rule.scope.value} "
            f"{rule.kind.value} {rule.pattern}")


def _highest_priority(matches: Sequence[RuleMatch]) -> Optional[RuleMatch]:
    for decision in (RuleDecision.DENY, RuleDecision.ASK, RuleDecision.ALLOW):
        for match in matches:
            if match.decision is decision:
                return match
    return None
