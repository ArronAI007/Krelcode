"""allow 规则持久化：用户批过 "always" 的程序名，下次不再询问。

粒度 = 命令的首个程序名（如 pytest、npm）。存储在
<workspace>/.krelcode/allow_rules.json，随项目走、可人工审查。
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import List, Optional, Set


def command_program(command: str) -> Optional[str]:
    """提取命令首个程序名（basename）；解析失败返回 None。"""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return None
    prog = tokens[0]
    return prog.rsplit("/", 1)[-1] if "/" in prog else prog


class AllowRules:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path
        self.programs: Set[str] = set()
        if path is not None and path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.programs = {str(p) for p in data.get("allow_programs", [])}
            except (OSError, json.JSONDecodeError):
                self.programs = set()  # 规则文件损坏 → 清空（fail-closed）

    def matches(self, command: str) -> bool:
        prog = command_program(command)
        return prog is not None and prog in self.programs

    def add(self, command: str) -> bool:
        """记录命令的程序名并落盘；返回是否新增了规则。"""
        prog = command_program(command)
        if prog is None:
            return False
        return self.add_program(prog)

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
            payload = {"allow_programs": sorted(self.programs)}
            self.path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass  # 持久化失败不阻断审批（仅本次会话有效）

    def as_list(self) -> List[str]:
        return sorted(self.programs)
