"""Skill 发现、渐进加载、资源目录与热重载。"""

from __future__ import annotations

import html
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, List, Optional, Tuple

from ..tools.secret_guard import classify_secret_path, redact_secret_text
from .models import Skill, SkillError, SkillIssue, SkillReloadReport, SkillResource
from .parser import SkillParseError, parse_skill_document, split_allowed_tools

MAX_SKILL_BYTES = 256_000
MAX_PROMPT_SKILLS = 100
MAX_PROMPT_CHARS = 12_000
MAX_DESCRIPTION_CHARS = 320
MAX_RESOURCES = 300
MAX_RESOURCE_BYTES = 200_000
MAX_RESOURCE_OUTPUT = 50_000
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

PROJECT_SKILL_ROOTS = (
    Path(".agents") / "skills",
    Path(".krelcode") / "skills",
)
GLOBAL_SKILL_ROOTS = (
    Path(".agents") / "skills",
    Path(".krelcode") / "skills",
)


@dataclass(frozen=True)
class _Root:
    path: Path
    label: str


class SkillManager:
    """按 global → project → custom 的优先级发现 Skill，后者覆盖同名前者。"""

    def __init__(
        self,
        workspace_root: Path,
        *,
        home: Optional[Path] = None,
        extra_paths: Optional[Iterable[str | Path]] = None,
        enabled: bool = True,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.home = (Path(home) if home is not None else Path.home()).expanduser().resolve()
        self.extra_paths = tuple(Path(p).expanduser() for p in (extra_paths or ()))
        self.enabled = enabled
        self._skills: dict[str, Skill] = {}
        self._issues: Tuple[SkillIssue, ...] = ()
        self._roots = self._build_roots()
        if enabled:
            self.reload()

    def _build_roots(self) -> List[_Root]:
        roots: List[_Root] = []
        for rel in GLOBAL_SKILL_ROOTS:
            roots.append(_Root(self.home / rel, f"global:{rel.as_posix()}"))
        for rel in PROJECT_SKILL_ROOTS:
            roots.append(_Root(self.workspace_root / rel, f"project:{rel.as_posix()}"))
        for raw in self.extra_paths:
            path = raw if raw.is_absolute() else self.workspace_root / raw
            roots.append(_Root(path.resolve(), f"custom:{raw}"))
        # 去重保序；后面的 root 覆盖前面的同名 skill。
        seen: set[Path] = set()
        unique: List[_Root] = []
        for root in roots:
            try:
                resolved = root.path.resolve()
            except OSError:
                resolved = root.path
            if resolved in seen:
                continue
            seen.add(resolved)
            unique.append(_Root(resolved, root.label))
        return unique

    def reload(self) -> SkillReloadReport:
        if not self.enabled:
            self._skills = {}
            self._issues = ()
            return SkillReloadReport((), ())
        found: dict[str, Skill] = {}
        issues: List[SkillIssue] = []
        for root in self._roots:
            if not root.path.is_dir():
                continue
            candidates = [root.path] if (root.path / "SKILL.md").is_file() else []
            try:
                candidates.extend(
                    p for p in sorted(root.path.iterdir(), key=lambda x: x.name.lower())
                    if p.is_dir() and (p / "SKILL.md").is_file()
                )
            except OSError as e:
                issues.append(SkillIssue(root.path, f"无法扫描目录: {e}"))
                continue
            for directory in candidates:
                try:
                    skill = self._load_metadata(directory, root.label)
                except (SkillParseError, OSError, ValueError) as e:
                    issues.append(SkillIssue(directory / "SKILL.md", str(e)))
                    continue
                found[skill.name] = skill
        self._skills = found
        self._issues = tuple(issues)
        return SkillReloadReport(tuple(self.list()), self._issues)

    def _load_metadata(self, directory: Path, source: str) -> Skill:
        directory = directory.resolve()
        skill_file = (directory / "SKILL.md").resolve()
        if directory not in skill_file.parents:
            raise ValueError("SKILL.md 越出 skill 目录")
        size = skill_file.stat().st_size
        if size > MAX_SKILL_BYTES:
            raise ValueError(f"SKILL.md 过大（{size} > {MAX_SKILL_BYTES} bytes）")
        text = skill_file.read_text(encoding="utf-8")
        metadata, _ = parse_skill_document(text, fallback_name=directory.name)
        name = str(metadata.get("name") or directory.name).strip()
        description = _one_line(str(metadata.get("description") or ""))
        if not NAME_RE.match(name):
            raise ValueError(f"非法 skill 名称: {name!r}")
        if not description:
            raise ValueError("frontmatter 缺少非空 description")
        return Skill(
            name=name,
            description=description,
            directory=directory,
            skill_file=skill_file,
            source=source,
            user_invocable=_as_bool(metadata.get("user-invocable"), True),
            disable_model_invocation=_as_bool(
                metadata.get("disable-model-invocation"), False),
            argument_hint=str(metadata.get("argument-hint") or ""),
            allowed_tools=split_allowed_tools(metadata.get("allowed-tools")),
            version=str(metadata.get("version") or ""),
            license=str(metadata.get("license") or ""),
            metadata=metadata,
        )

    def list(
        self,
        *,
        query: str = "",
        model_invocable_only: bool = False,
        user_invocable_only: bool = False,
    ) -> List[Skill]:
        items = list(self._skills.values())
        if model_invocable_only:
            items = [s for s in items if not s.disable_model_invocation]
        if user_invocable_only:
            items = [s for s in items if s.user_invocable]
        query = query.strip().lower()
        if query:
            items = [
                s for s in items
                if query in s.name.lower() or query in s.description.lower()
            ]
        return sorted(items, key=lambda s: s.name.lower())

    def get(self, name: str) -> Optional[Skill]:
        return self._skills.get(str(name).strip())

    @property
    def issues(self) -> Tuple[SkillIssue, ...]:
        return self._issues

    def prompt_index(self) -> str:
        skills = self.list(model_invocable_only=True)[:MAX_PROMPT_SKILLS]
        if not skills:
            return ""
        lines = [
            "<available_skills>",
            "Skills are specialized instruction packages. Select one only when its "
            "description matches the task. Call list_skills for details, then "
            "load_skill before following it. Use list_skill_resources/"
            "read_skill_resource for referenced files; bundled scripts must be run "
            "with run_skill_script.",
        ]
        used = sum(len(line) + 1 for line in lines)
        for skill in skills:
            description = html.escape(
                skill.description[:MAX_DESCRIPTION_CHARS], quote=False)
            line = f"- {skill.name}: {description}"
            if used + len(line) + 1 > MAX_PROMPT_CHARS:
                lines.append("- …[更多 skill 请调用 list_skills]")
                break
            lines.append(line)
            used += len(line) + 1
        lines.append("</available_skills>")
        return "\n".join(lines)

    def apply_to_prompt(self, base_prompt: str) -> str:
        index = self.prompt_index()
        return f"{base_prompt.rstrip()}\n\n{index}\n" if index else base_prompt

    def load(self, name: str, arguments: str = "", *, for_model: bool = True) -> str:
        skill = self._require_skill(name)
        if for_model and skill.disable_model_invocation:
            raise SkillError(f"skill {name!r} 禁止模型主动调用")
        if not for_model and not skill.user_invocable:
            raise SkillError(f"skill {name!r} 未开放用户调用")
        body = self._read_skill_body(skill)
        has_arguments_placeholder = (
            "$ARGUMENTS" in body or "${ARGUMENTS}" in body)
        variables = {
            "${SKILL_DIR}": str(skill.directory),
            "$SKILL_DIR": str(skill.directory),
            "${ARGUMENTS}": arguments,
            "$ARGUMENTS": arguments,
        }
        for key, value in variables.items():
            body = body.replace(key, value)
        if arguments and not has_arguments_placeholder:
            body = body.rstrip() + "\n\n## User arguments\n\n" + arguments
        lines = [
            f'<skill name="{skill.name}" '
            f'source="{html.escape(skill.source, quote=True)}">',
            f"Location: {html.escape(str(skill.directory), quote=False)}",
        ]
        if skill.allowed_tools:
            lines.append("Allowed tools guidance: " + ", ".join(skill.allowed_tools))
        resources = self.list_resources(skill.name)[:30]
        if resources:
            lines.append("Resources:")
            lines.extend(f"- {r.kind}: {r.path}" for r in resources)
        lines.extend([body.rstrip(), "</skill>"])
        return "\n".join(lines)

    def user_prompt(self, name: str, arguments: str = "") -> str:
        loaded = self.load(name, arguments, for_model=False)
        request = arguments.strip() or "请按照该 skill 的说明执行。"
        return f"{loaded}\n\nUser request:\n{request}"

    def list_resources(
        self, name: str, subpath: str = "", max_depth: int = 3,
    ) -> List[SkillResource]:
        skill = self._require_skill(name)
        base = self._resolve_in_skill(skill, subpath or ".")
        if not base.is_dir():
            raise SkillError(f"资源目录不存在: {subpath}")
        max_depth = max(1, min(int(max_depth or 3), 8))
        root = skill.directory.resolve()
        entries: List[SkillResource] = []
        try:
            walker = os.walk(base, followlinks=False)
            for current, dirs, files in walker:
                current_path = Path(current)
                rel_depth = len(current_path.relative_to(base).parts)
                dirs[:] = [
                    d for d in sorted(dirs)
                    if not d.startswith(".") and rel_depth < max_depth
                ]
                for filename in sorted(files):
                    path = current_path / filename
                    if filename.startswith(".") or path == skill.skill_file:
                        continue
                    if path.is_symlink() and not _inside(path.resolve(), root):
                        continue
                    rel = path.relative_to(root).as_posix()
                    entries.append(SkillResource(rel, "file", path.stat().st_size))
                    if len(entries) >= MAX_RESOURCES:
                        return entries
        except OSError as e:
            raise SkillError(f"无法列出资源: {e}") from e
        return entries

    def read_resource(
        self,
        name: str,
        resource_path: str,
        *,
        offset: int = 1,
        limit: int = 2000,
    ) -> str:
        skill = self._require_skill(name)
        path = self._resolve_in_skill(skill, resource_path)
        if not path.is_file():
            raise SkillError(f"资源文件不存在: {resource_path}")
        if path.stat().st_size > MAX_RESOURCE_BYTES:
            raise SkillError(f"资源文件过大（>{MAX_RESOURCE_BYTES} bytes）")
        category = classify_secret_path(path, skill.directory)
        if category == "block":
            raise SkillError(f"拒绝读取敏感资源: {path.name}")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            raise SkillError(f"资源不是 UTF-8 文本: {resource_path}") from e
        except OSError as e:
            raise SkillError(f"读取资源失败: {e}") from e
        if category in ("redact_env", "redact_toml"):
            text = redact_secret_text(text, category)
        return _render_lines(text, int(offset or 1), int(limit or 2000))

    def resolve_script(self, name: str, script_path: str) -> Tuple[Skill, Path]:
        skill = self._require_skill(name)
        path = self._resolve_in_skill(skill, script_path)
        if not path.is_file():
            raise SkillError(f"脚本不存在: {script_path}")
        category = classify_secret_path(path, skill.directory)
        if category:
            raise SkillError(f"拒绝执行敏感文件: {path.name}")
        return skill, path

    def _read_skill_body(self, skill: Skill) -> str:
        if skill.skill_file.stat().st_size > MAX_SKILL_BYTES:
            raise SkillError("SKILL.md 已超过大小限制")
        try:
            text = skill.skill_file.read_text(encoding="utf-8")
        except OSError as e:
            raise SkillError(f"读取 SKILL.md 失败: {e}") from e
        metadata, body = parse_skill_document(text, fallback_name=skill.directory.name)
        if str(metadata.get("name") or skill.directory.name) != skill.name:
            raise SkillError("SKILL.md 的 name 已变化，请先 /skills reload")
        return body

    def _require_skill(self, name: str) -> Skill:
        skill = self.get(name)
        if skill is None:
            raise SkillError(f"未知 skill: {name}")
        return skill

    def _resolve_in_skill(self, skill: Skill, raw: str) -> Path:
        if not raw or not str(raw).strip():
            raise SkillError("资源路径不能为空")
        candidate = Path(raw)
        if candidate.is_absolute():
            raise SkillError("资源路径必须是 skill 目录内的相对路径")
        base = skill.directory.resolve()
        resolved = (base / candidate).resolve()
        if not _inside(resolved, base):
            raise SkillError("资源路径越出 skill 目录")
        return resolved


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    lowered = str(value).strip().lower()
    if lowered in {"true", "yes", "on", "1"}:
        return True
    if lowered in {"false", "no", "off", "0"}:
        return False
    return default


def _render_lines(text: str, offset: int, limit: int) -> str:
    offset = max(1, offset)
    limit = max(1, min(limit, 2000))
    lines = text.splitlines()
    if not lines:
        return "（空文件）"
    if offset > len(lines):
        return f"offset={offset} 超出文件总行数 {len(lines)}"
    out: List[str] = []
    chars = 0
    last = offset - 1
    for i in range(offset - 1, min(offset - 1 + limit, len(lines))):
        line = lines[i]
        rendered = f"{i + 1:6d}→{line}"
        if chars + len(rendered) > MAX_RESOURCE_OUTPUT:
            break
        out.append(rendered)
        chars += len(rendered) + 1
        last = i + 1
    note = ""
    if last < len(lines):
        note = f"\n[显示第 {offset}-{last} 行，共 {len(lines)} 行；继续读取 offset={last + 1}]"
    return "\n".join(out) + note
