"""allow 规则持久化：精确命令规则 + 手工程序级规则。

“始终允许”默认只保存规范化后的精确命令，避免一次批准 `git fetch` 后自动
放行 `git push`，或批准 `pytest && ...` 后自动放行其他复合命令。用户仍可
通过 `/allow add <program>` 显式建立程序级规则；程序级规则只匹配单一、无
包装、无重定向的直接命令。

规则存储在 <workspace>/.krelcode/allow_rules.json，随项目走、可人工审查。
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import List, Optional, Set

from .commands import REDIRECTS, SEPARATORS, _tokenize

_WRAPPER_PROGRAMS = {
    "env", "command", "exec", "nohup", "nice", "time", "timeout", "xargs",
}
_PROBABLE_SECRET = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|auth(?:orization)?|password|"
    r"client[_-]?secret|private[_-]?key|secret)[\"']?\s*[:=]\s*\S+"
)


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


class AllowRules:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path
        self.programs: Set[str] = set()
        self.commands: Set[str] = set()
        if path is not None and path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.programs = {
                    str(item) for item in data.get("allow_programs", [])
                }
                self.commands = {
                    str(item) for item in data.get("allow_commands", [])
                }
            except (OSError, json.JSONDecodeError):
                self.programs = set()  # 规则文件损坏 → 清空（fail-closed）
                self.commands = set()

    def matches(self, command: str) -> bool:
        key = command_key(command)
        if key is not None and key in self.commands:
            return True
        prog = _simple_program(command)
        return prog is not None and prog in self.programs

    def add(self, command: str) -> bool:
        """记录精确命令并落盘；返回是否新增了规则。"""
        key = command_key(command)
        if key is None or key in self.commands:
            return False
        self.commands.add(key)
        self._save()
        return True

    def add_program(self, prog: str) -> bool:
        """直接按程序名添加规则（/allow add 用）；返回是否新增。"""
        prog = (prog or "").strip()
        if not prog or prog in self.programs:
            return False
        self.programs.add(prog)
        self._save()
        return True

    def remove_program(self, prog: str) -> bool:
        """移除规则（/allow remove 用）；返回是否确实移除了。"""
        prog = (prog or "").strip()
        if prog not in self.programs:
            return False
        self.programs.discard(prog)
        self._save()
        return True

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "allow_programs": sorted(self.programs),
                "allow_commands": sorted(self.commands),
            }
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
