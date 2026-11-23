"""Static database-migration inspection, risk analysis, and safe scaffolding.

No tool in this module opens a database connection or executes a migration.
Framework discovery and SQL/Python inspection are bounded, path-confined, and
content-redacted. Applying or rolling back a migration remains an explicit
``run_command`` operation governed by ApprovalPolicy, Sandbox, and the project's
framework-native commands.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from ...core.redact import redact_obvious_secrets
from ..base import ToolContext, ToolResult, ToolTier
from ..paths import (
    FileChangedError,
    PathOutsideWorkspaceError,
    UnsafeFileError,
    atomic_write_text,
    read_text_file_with_snapshot,
    resolve_in_workspace,
    safe_file_in_workspace,
)
from ..prompt import ToolPromptExample
from ..secret_guard import classify_secret_path

SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", ".krelcode", "node_modules", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv", "venv",
    "env", "dist", "build", "target", ".idea", ".vscode",
})
MIGRATION_EXTENSIONS = frozenset({".sql", ".py", ".rb", ".xml", ".yaml", ".yml", ".json"})
MAX_DISCOVERY_FILES = 10_000
MAX_INSPECT_CHARS = 60_000
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
_REVISION_RE = re.compile(
    r"(?m)^\s*revision(?:\s*:\s*[^=]+)?\s*=\s*['\"]([^'\"]+)['\"]")
_DOWN_REVISION_RE = re.compile(
    r"(?m)^\s*down_revision(?:\s*:\s*[^=]+)?\s*=\s*(.+)$")


@dataclass(frozen=True)
class MigrationOptions:
    enabled: bool = True
    max_files: int = 300
    max_file_bytes: int = 1_000_000
    max_output_chars: int = 40_000

    @classmethod
    def from_config(cls, section: Any = None) -> "MigrationOptions":
        value = section if isinstance(section, Mapping) else {}
        return cls(
            enabled=bool(value.get("enabled", True)),
            max_files=max(1, min(
                _positive_int(value.get("max_files"), 300), 2_000)),
            max_file_bytes=max(64 * 1024, min(
                _positive_int(value.get("max_file_bytes"), 1_000_000),
                10_000_000)),
            max_output_chars=max(2_000, min(
                _positive_int(value.get("max_output_chars"), 40_000),
                100_000)),
        )


@dataclass(frozen=True)
class MigrationDiscovery:
    root: Path
    files: Tuple[Path, ...]
    migration_files: Tuple[Path, ...]
    frameworks: Tuple[str, ...]
    alembic_heads: Tuple[str, ...] = ()
    alembic_revisions: Tuple[str, ...] = ()


@dataclass(frozen=True)
class MigrationFinding:
    path: str
    line: int
    code: str
    severity: str
    message: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "line": self.line,
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
        }


class MigrationInspectTool:
    read_only = True
    tool_tier = ToolTier.DISCOVERABLE
    name = "migration_inspect"
    description = (
        "静态识别 Alembic、Django、Prisma、Flyway、Liquibase、Rails 与通用 SQL "
        "迁移目录，报告文件、revision head 和需要执行的原生命令；不连接数据库。"
    )
    prompt = """\
Use migration_inspect before editing a migration or planning an application
command. It discovers project-local migration frameworks and revision files
without opening a database. Treat the reported head as a file-graph fact, not as
the live database state.

Do not read .env/credential files or infer current production state. To inspect
live state, propose the framework's read-only command (for example
`alembic current`, `python manage.py showmigrations`, or `prisma migrate status`)
through run_command under Sandbox and approval.
"""
    when_to_use = (
        "A task touches schema, migration history, or database state.",
        "The migration framework or current file revision graph is unknown.",
    )
    when_not_to_use = (
        "The project has no database schema or migration scope.",
        "Live database state is required; inspect files, then use an approved framework command.",
    )
    prompt_examples = (
        ToolPromptExample(
            title="Inspect project migration layout",
            input={"path": "."},
            description=(
                "Discover migration frameworks and file heads without reading "
                "credentials or connecting to a database."
            ),
        ),
    )
    prompt_version = "1"
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "maxLength": 500},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, options: MigrationOptions) -> None:
        self.options = options

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            discovery = _discover(ctx, str(args.get("path") or "."), self.options)
        except (PathOutsideWorkspaceError, ValueError, OSError) as exc:
            return ToolResult(content=f"迁移检查失败: {exc}", is_error=True)
        lines = [
            (
                f'<database_migrations trust="project_data" '
                f'root={json.dumps(str(discovery.root))} '
                f'frameworks={json.dumps(list(discovery.frameworks))}>'
            ),
            "Database state: not queried; no connection was attempted.",
        ]
        if discovery.frameworks:
            lines.append("Detected frameworks: " + ", ".join(discovery.frameworks))
        else:
            lines.append("Detected frameworks: none")
        if discovery.alembic_revisions:
            lines.append(
                "Alembic file revisions: " + ", ".join(discovery.alembic_revisions))
            lines.append(
                "Alembic file heads: "
                + (", ".join(discovery.alembic_heads) if discovery.alembic_heads else "unknown"))
        lines.append(f"Migration files: {len(discovery.migration_files)}")
        for path in discovery.migration_files[:self.options.max_files]:
            lines.append(f"- {_relative(path, discovery.root)}")
        if len(discovery.migration_files) > self.options.max_files:
            lines.append(
                f"…[另有 {len(discovery.migration_files) - self.options.max_files} 个文件]")
        commands = _suggest_state_commands(discovery.frameworks)
        if commands:
            lines.append("Read-only state commands to consider via run_command:")
            lines.extend(f"- {command}" for command in commands)
        lines.append("</database_migrations>")
        content = "\n".join(lines)
        return ToolResult(
            content=content[:self.options.max_output_chars],
            data={
                "root": str(discovery.root),
                "frameworks": list(discovery.frameworks),
                "migration_files": [
                    _relative(path, discovery.root)
                    for path in discovery.migration_files[:self.options.max_files]
                ],
                "alembic_heads": list(discovery.alembic_heads),
                "alembic_revisions": list(discovery.alembic_revisions),
                "database_connected": False,
            },
        )


class MigrationValidateTool:
    read_only = True
    tool_tier = ToolTier.DISCOVERABLE
    name = "migration_validate"
    description = (
        "静态分析迁移文件中的破坏性 DDL/DML、锁表风险、不可逆操作与缺失回滚证据。"
        "不连接数据库，也不执行迁移。"
    )
    prompt = """\
Use migration_validate after identifying the migration scope and before applying
or approving a schema/data change. It statically examines bounded migration
files and reports line-specific risks; absence of findings is not proof of
safety, reversibility, data compatibility, or runtime correctness.

Always inspect every critical/high finding. A rollback file name is only evidence
that a rollback artifact exists, not proof that it runs successfully. Forward,
down/rollback, schema state, and representative data must be verified with
project-native commands against a disposable database when available.
"""
    when_to_use = (
        "A migration file or migration directory must be risk-reviewed.",
        "Destructive DDL/DML, lock behavior, or rollback evidence needs inspection.",
    )
    when_not_to_use = (
        "Live migration state must be queried; use an approved framework command.",
        "The user asks to apply the migration; validation alone performs no mutation.",
    )
    prompt_examples = (
        ToolPromptExample(
            title="Review one migration directory",
            input={"path": "migrations/versions"},
            description=(
                "Statically review bounded migration files and then verify "
                "rollback behavior with framework-native commands."
            ),
        ),
    )
    prompt_version = "1"
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "maxLength": 500},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, options: MigrationOptions) -> None:
        self.options = options

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            discovery = _discover(ctx, str(args.get("path") or "."), self.options)
            findings = _validate_migrations(discovery, self.options)
        except (PathOutsideWorkspaceError, ValueError, OSError) as exc:
            return ToolResult(content=f"迁移验证失败: {exc}", is_error=True)
        counts = Counter(finding.severity for finding in findings)
        lines = [
            (
                f'<migration_validate trust="project_data" '
                f'root={json.dumps(str(discovery.root))} '
                f'frameworks={json.dumps(list(discovery.frameworks))}>'
            ),
            (
                f"Files checked: {len(discovery.migration_files)} · "
                f"critical={counts['critical']} high={counts['high']} "
                f"medium={counts['medium']} low={counts['low']}"
            ),
            "No database connection or migration execution was performed.",
        ]
        if findings:
            for finding in findings:
                lines.append(
                    f"[{finding.severity.upper()}] {finding.code} "
                    f"{finding.path}:{finding.line} {finding.message}")
        else:
            lines.append("No static risk pattern was found; this is not proof of runtime safety.")
        lines.append("</migration_validate>")
        content = "\n".join(lines)
        return ToolResult(
            content=content[:self.options.max_output_chars],
            data={
                "root": str(discovery.root),
                "frameworks": list(discovery.frameworks),
                "files_checked": len(discovery.migration_files),
                "counts": dict(counts),
                "findings": [finding.as_dict() for finding in findings],
                "database_connected": False,
            },
            is_error=counts["critical"] > 0,
        )


class MigrationCreateTool:
    tool_tier = ToolTier.DISCOVERABLE
    name = "migration_create"
    description = (
        "创建带显式 TODO 的 Alembic、Flyway、Prisma 或通用 SQL 迁移脚手架。"
        "不会生成数据库变更、连接数据库或执行迁移。"
    )
    prompt = """\
Prefer the project's framework-native generator command when one exists. Use
migration_create only for a new scaffold whose directory and filename are known,
and never overwrite a migration file.

A scaffold is incomplete until its forward and rollback logic are implemented and
validated. Alembic scaffolds intentionally raise NotImplementedError in both
directions to prevent accidental no-op application. Flyway/Prisma/generic SQL
scaffolds contain comments only and must be completed before use. Applying or
rolling back a scaffold still requires a separately approved run_command.
"""
    when_to_use = (
        "A new migration scaffold must be created in an existing migration tree.",
        "No native generator is available for the selected supported framework.",
    )
    when_not_to_use = (
        "The user expects the migration to be implemented or applied immediately.",
        "The file already exists or the project uses an unsupported migration framework.",
    )
    prompt_examples = (
        ToolPromptExample(
            title="Create a generic SQL scaffold",
            input={
                "framework": "generic",
                "name": "add_account_status",
                "path": "migrations",
            },
            description=(
                "Create a timestamped comment-only SQL scaffold, then implement "
                "and verify forward/rollback behavior separately."
            ),
        ),
    )
    prompt_version = "1"
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "framework": {
                "type": "string",
                "enum": ["alembic", "flyway", "prisma", "generic"],
            },
            "name": {"type": "string", "minLength": 2, "maxLength": 64},
            "path": {"type": "string", "maxLength": 500},
        },
        "required": ["framework", "name"],
        "additionalProperties": False,
    }

    def __init__(self, options: MigrationOptions) -> None:
        self.options = options

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        framework = str(args.get("framework") or "").strip().lower()
        if framework not in {"alembic", "flyway", "prisma", "generic"}:
            return ToolResult(content="不支持的迁移框架", is_error=True)
        name = str(args.get("name") or "").strip()
        if not _NAME_RE.fullmatch(name):
            return ToolResult(
                content="name 必须是 2-64 位小写字母、数字、连字符或下划线",
                is_error=True,
            )
        try:
            discovery = _discover(ctx, ".", self.options)
            path_value = str(args.get("path") or "").strip()
            output_path, content, note = _build_migration_scaffold(
                ctx, framework, name, path_value, discovery)
            if output_path.exists():
                return ToolResult(content=f"迁移文件已存在: {output_path}", is_error=True)
            atomic_write_text(output_path, content)
            return ToolResult(
                content=(
                    f"已创建迁移脚手架: {output_path}\n{note}\n"
                    "尚未连接数据库或应用迁移；完成实现后先运行 dry-run/plan 与回滚验证。"
                ),
                data={
                    "path": str(output_path),
                    "framework": framework,
                    "created": True,
                    "database_connected": False,
                },
            )
        except (PathOutsideWorkspaceError, ValueError, OSError,
                UnsafeFileError, FileChangedError) as exc:
            return ToolResult(content=f"迁移脚手架创建失败: {exc}", is_error=True)


def build_migration_tools(options: MigrationOptions) -> list[Any]:
    if not options.enabled:
        return []
    return [
        MigrationInspectTool(options),
        MigrationValidateTool(options),
        MigrationCreateTool(options),
    ]


def _discover(
    ctx: ToolContext, raw_path: str, options: MigrationOptions,
) -> MigrationDiscovery:
    target = resolve_in_workspace(ctx, raw_path or ".")
    if target.is_file():
        files = (target,)
        root = target.parent
    elif target.is_dir():
        root = target
        files = _walk_files(ctx, root, options)
    else:
        raise ValueError(f"路径不存在: {target}")

    relative = tuple((path, _relative(path, root)) for path in files)
    migration_files = tuple(
        path for path, rel in relative
        if _looks_like_migration_file(path, rel, root=root))
    frameworks = _detect_frameworks(relative, migration_files, root=root)
    revisions, heads = _alembic_graph(root, migration_files, options)
    return MigrationDiscovery(
        root=root,
        files=tuple(files),
        migration_files=tuple(sorted(migration_files, key=lambda item: str(item))),
        frameworks=tuple(sorted(frameworks)),
        alembic_heads=tuple(sorted(heads)),
        alembic_revisions=tuple(sorted(revisions)),
    )


def _walk_files(
    ctx: ToolContext, root: Path, options: MigrationOptions,
) -> Tuple[Path, ...]:
    results: list[Path] = []
    for current, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(
            name for name in dirs
            if name not in SKIP_DIRS
            and not (Path(current) / name).is_symlink()
        )
        for name in sorted(names):
            path = Path(current) / name
            if path.is_symlink():
                continue
            if classify_secret_path(path, ctx.workspace_root) is not None:
                continue
            if not safe_file_in_workspace(path, ctx.workspace_root):
                continue
            results.append(path)
            if len(results) >= MAX_DISCOVERY_FILES:
                return tuple(results)
    return tuple(results)


def _looks_like_migration_file(
    path: Path, rel: str, *, root: Optional[Path] = None,
) -> bool:
    lower = (
        f"{root.name}/{rel}" if root is not None else rel
    ).casefold()
    name = path.name.casefold()
    suffix = path.suffix.casefold()
    if suffix not in MIGRATION_EXTENSIONS:
        return False
    if "migration" in lower or "/migrate/" in f"/{lower}" or "changelog" in name:
        return True
    if name in {"schema.prisma", "structure.sql"}:
        return False
    if re.fullmatch(r"[vru]\d+.*\.sql", name):
        return True
    if "/versions/" in f"/{lower}" and suffix == ".py":
        return True
    return False


def _detect_frameworks(
    files: Sequence[Tuple[Path, str]], migration_files: Sequence[Path],
    *, root: Optional[Path] = None,
) -> set[str]:
    prefix = f"{root.name.casefold()}/" if root is not None else ""
    rels = {
        (prefix + rel if prefix else rel).casefold()
        for _path, rel in files
    }
    names = {Path(rel).name.casefold() for rel in rels}
    frameworks: set[str] = set()
    if "alembic.ini" in names or any(
            ("/versions/" in f"/{rel}" and rel.endswith(".py")) for rel in rels):
        frameworks.add("alembic")
    if any(
            re.fullmatch(r"\d{4}_.+\.py", Path(rel).name)
            and "/migrations/" in f"/{rel}"
            for rel in rels):
        frameworks.add("django")
    if "schema.prisma" in names or any(
            rel.endswith("/migration.sql") and "/prisma/" in f"/{rel}"
            for rel in rels):
        frameworks.add("prisma")
    if any(
            re.fullmatch(r"[vru]\d+.*\.sql", Path(rel).name)
            for _path, rel in files):
        frameworks.add("flyway")
    if any("changelog" in name and Path(name).suffix in {".xml", ".yaml", ".yml", ".json"}
           for name in names):
        frameworks.add("liquibase")
    if any(rel.endswith(".rb") and "/db/migrate/" in f"/{rel}" for rel in rels):
        frameworks.add("rails")
    if migration_files and not frameworks:
        frameworks.add("generic-sql")
    return frameworks


def _alembic_graph(
    root: Path, migration_files: Sequence[Path], options: MigrationOptions,
) -> Tuple[set[str], set[str]]:
    revisions: set[str] = set()
    down_refs: set[str] = set()
    for path in migration_files:
        rel = _relative(path, root)
        is_versions_file = (
            "/versions/" in f"/{rel.casefold()}"
            or root.name.casefold() == "versions"
        )
        if path.suffix != ".py" or not is_versions_file:
            continue
        text = _read_bounded(path, options.max_file_bytes)
        if text is None:
            continue
        for match in _REVISION_RE.finditer(text):
            revisions.add(match.group(1).strip())
        for match in _DOWN_REVISION_RE.finditer(text):
            raw = match.group(1).split("#", 1)[0]
            for value in re.findall(r"['\"]([^'\"]+)['\"]", raw):
                down_refs.add(value.strip())
    return revisions, revisions - down_refs


def _validate_migrations(
    discovery: MigrationDiscovery, options: MigrationOptions,
) -> Tuple[MigrationFinding, ...]:
    findings: list[MigrationFinding] = []
    seen: set[tuple[str, int, str]] = set()
    for path in discovery.migration_files[:options.max_files]:
        text = _read_bounded(path, options.max_file_bytes)
        if text is None:
            continue
        rel = _relative(path, discovery.root)
        scrubbed = _scrub_comments(text, path.suffix)
        local_before = len(findings)
        for pattern, code, severity, message in _RISK_PATTERNS:
            for match in pattern.finditer(scrubbed):
                _add_finding(
                    findings, seen, rel, scrubbed, match.start(),
                    code, severity, message)
        for match in _statement_re.finditer(scrubbed):
            statement = match.group(0)
            lower = statement.casefold()
            if lower.startswith("delete") and not re.search(r"\bwhere\b", lower):
                _add_finding(
                    findings, seen, rel, scrubbed, match.start(),
                    "MIG101", "critical",
                    "DELETE 没有 WHERE，可能清空整表")
            elif lower.startswith("update") and re.search(r"\bset\b", lower) and not re.search(
                    r"\bwhere\b", lower):
                _add_finding(
                    findings, seen, rel, scrubbed, match.start(),
                    "MIG102", "critical",
                    "UPDATE 没有 WHERE，可能覆盖整表")
        if path.suffix in {".py", ".rb"}:
            for pattern, code, severity, message in _CODE_RISK_PATTERNS:
                for match in pattern.finditer(scrubbed):
                    _add_finding(
                        findings, seen, rel, scrubbed, match.start(),
                        code, severity, message)
        framework = _framework_for_path(
            f"{discovery.root.name}/{rel}" if discovery.root.name else rel)
        if framework == "alembic":
            rollback = _alembic_downgrade_state(scrubbed)
            if rollback != "implemented":
                line = _line_at(scrubbed, scrubbed.find("def downgrade") if "def downgrade" in scrubbed else 0)
                _add_finding(
                    findings, seen, rel, scrubbed, _offset_for_line(scrubbed, line) if "def downgrade" in scrubbed else 0,
                    "MIG201", "high",
                    "Alembic downgrade 缺失或未实现；必须提供可运行的回滚路径")
        destructive = any(
            item.path == rel and item.severity in {"critical", "high"}
            for item in findings
        )
        if destructive and not _has_rollback_artifact(path, framework):
            _add_finding(
                findings, seen, rel, scrubbed, 0,
                "MIG202", "low",
                "未找到与高风险变更配套的 down/undo/rollback 证据")
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    return tuple(sorted(
        findings,
        key=lambda item: (
            severity_order.get(item.severity, 9), item.path, item.line, item.code)))


_RISK_PATTERNS: tuple[tuple[re.Pattern[str], str, str, str], ...] = (
    (re.compile(r"\bdrop\s+database\b", re.I), "MIG001", "critical", "DROP DATABASE 会删除整个数据库"),
    (re.compile(r"\bdrop\s+table\b", re.I), "MIG002", "critical", "DROP TABLE 会删除表及数据"),
    (re.compile(r"\btruncate\b", re.I), "MIG003", "high", "TRUNCATE 会不可逆地清空数据"),
    (re.compile(r"\bdrop\s+column\b", re.I), "MIG004", "high", "DROP COLUMN 会删除列及数据"),
    (re.compile(
        r"\badd\s+(?:column\s+)?[^;]*\bnot\s+null\b(?![^;]*\bdefault\b)",
        re.I | re.S,
    ), "MIG005", "high", "新增 NOT NULL 列但未提供 DEFAULT，现有数据可能失败"),
    (re.compile(r"\balter\s+column\b[^;]*\btype\b", re.I | re.S), "MIG006", "high", "ALTER COLUMN TYPE 可能重写表并造成兼容性风险"),
    (re.compile(r"\bcreate\s+(?:unique\s+)?index\b(?!\s+concurrently\b)", re.I), "MIG007", "medium", "创建索引未使用 CONCURRENTLY，可能持有锁"),
    (re.compile(r"\bdrop\s+constraint\b", re.I), "MIG008", "high", "DROP CONSTRAINT 可能破坏完整性约束"),
)
_CODE_RISK_PATTERNS: tuple[tuple[re.Pattern[str], str, str, str], ...] = (
    (re.compile(r"\bop\.drop_table\s*\(", re.I), "MIG101", "critical", "Alembic op.drop_table 会删除表"),
    (re.compile(r"\bop\.drop_column\s*\(", re.I), "MIG102", "high", "Alembic op.drop_column 会删除列"),
    (re.compile(r"\bop\.drop_constraint\s*\(", re.I), "MIG103", "high", "Alembic op.drop_constraint 会删除约束"),
    (re.compile(r"\bop\.execute\s*\([^)]*\btruncate\b", re.I | re.S), "MIG104", "high", "迁移执行了 TRUNCATE"),
)
_statement_re = re.compile(r"(?is)\b(?:delete\s+from|update)\b.*?(?:;|$)")


def _add_finding(
    findings: list[MigrationFinding], seen: set[tuple[str, int, str]],
    rel: str, text: str, offset: int, code: str, severity: str, message: str,
) -> None:
    line = _line_at(text, max(0, offset))
    key = (rel, line, code)
    if key in seen:
        return
    seen.add(key)
    findings.append(MigrationFinding(
        path=rel, line=line, code=code, severity=severity,
        message=redact_obvious_secrets(message)))


def _scrub_comments(text: str, suffix: str) -> str:
    chars = list(text)
    index = 0
    state = "code"
    while index < len(chars):
        char = chars[index]
        nxt = chars[index + 1] if index + 1 < len(chars) else ""
        if state == "code":
            if char == "-" and nxt == "-":
                chars[index] = chars[index + 1] = " "
                state = "line"
                index += 2
                continue
            if char == "/" and nxt == "*":
                chars[index] = chars[index + 1] = " "
                state = "block"
                index += 2
                continue
            if char == "#" and suffix in {".py", ".rb", ".yaml", ".yml"}:
                chars[index] = " "
                state = "line"
                index += 1
                continue
            if char in {"'", '"'}:
                state = char
        elif state == "line":
            if char == "\n":
                state = "code"
            else:
                chars[index] = " "
        elif state == "block":
            if char == "*" and nxt == "/":
                chars[index] = chars[index + 1] = " "
                state = "code"
                index += 2
                continue
            if char != "\n":
                chars[index] = " "
        elif state in {"'", '"'}:
            if char == "\\":
                index += 2
                continue
            if char == state:
                state = "code"
        index += 1
    return "".join(chars)


def _line_at(text: str, offset: int) -> int:
    return text.count("\n", 0, max(0, offset)) + 1


def _offset_for_line(text: str, line: int) -> int:
    if line <= 1:
        return 0
    offset = 0
    for _ in range(line - 1):
        found = text.find("\n", offset)
        if found < 0:
            return len(text)
        offset = found + 1
    return offset


def _framework_for_path(rel: str) -> str:
    lower = rel.casefold()
    if "/versions/" in f"/{lower}" and lower.endswith(".py"):
        return "alembic"
    if re.fullmatch(r"\d{4}_.+\.py", Path(lower).name) and "/migrations/" in f"/{lower}":
        return "django"
    if lower.endswith("/migration.sql") and "/prisma/" in f"/{lower}":
        return "prisma"
    if re.fullmatch(r"[vru]\d+.*\.sql", Path(lower).name):
        return "flyway"
    if lower.endswith(".rb") and "/db/migrate/" in f"/{lower}":
        return "rails"
    if "changelog" in Path(lower).name:
        return "liquibase"
    return "generic"


def _alembic_downgrade_state(text: str) -> str:
    match = re.search(r"(?ms)^def\s+downgrade\s*\([^)]*\)\s*:\s*(.*?)(?=^def\s+|\Z)", text)
    if match is None:
        return "missing"
    body = "\n".join(
        line for line in match.group(1).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ).strip()
    if not body or re.fullmatch(r"(?:pass\s*)+", body):
        return "empty"
    if re.search(r"\bNotImplemented(?:Error)?\b|raise\s+NotImplementedError", body):
        return "unimplemented"
    return "implemented"


def _has_rollback_artifact(path: Path, framework: str) -> bool:
    try:
        candidates = {
            item.name.casefold()
            for item in path.parent.iterdir()
            if item.is_file()
        }
    except OSError:
        candidates = set()
    name = path.name.casefold()
    if framework == "flyway":
        if name.startswith("v"):
            return name.replace("v", "u", 1) in candidates
        return any("undo" in item or "rollback" in item for item in candidates)
    if framework == "alembic":
        return True  # downgrade state is reported separately
    if framework == "rails":
        return True  # change/down method is checked at runtime
    if framework == "liquibase":
        return True  # rollback may be centralized in a changelog
    stem = path.stem.casefold()
    return any(
        item != name and stem in item
        and any(marker in item for marker in ("down", "undo", "rollback"))
        for item in candidates
    )


def _build_migration_scaffold(
    ctx: ToolContext, framework: str, name: str, raw_path: str,
    discovery: MigrationDiscovery,
) -> tuple[Path, str, str]:
    directory = _scaffold_directory(ctx, framework, raw_path, discovery)
    if directory.exists() and not directory.is_dir():
        raise ValueError(f"目标路径不是目录: {directory}")
    if classify_secret_path(directory, ctx.workspace_root) is not None:
        raise ValueError("拒绝写入敏感路径")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    if framework == "alembic":
        revisions, heads = _alembic_graph(directory, _python_files(directory), MigrationOptions())
        if len(heads) > 1:
            raise ValueError(
                "Alembic 有多个 head，先合并分支再创建迁移: " + ", ".join(sorted(heads)))
        revision = uuid.uuid4().hex[:12]
        down_revision = next(iter(heads)) if heads else None
        path = directory / f"{timestamp}_{name}.py"
        content = (
            f'"""{name} migration scaffold."""\n\n'
            "from alembic import op\n"
            "import sqlalchemy as sa\n\n"
            f'revision = "{revision}"\n'
            f'down_revision = {down_revision!r}\n'
            "branch_labels = None\n"
            "depends_on = None\n\n\n"
            "def upgrade() -> None:\n"
            '    raise NotImplementedError("implement the forward migration")\n\n\n'
            "def downgrade() -> None:\n"
            '    raise NotImplementedError("implement a verified rollback")\n'
        )
        return path, content, "Alembic upgrade/downgrade 均会显式失败，完成实现前不可应用。"
    if framework == "flyway":
        path = directory / f"V{timestamp}__{name}.sql"
        content = (
            f"-- Krelcode migration scaffold: {name}\n"
            "-- Replace this comment with the forward migration SQL.\n"
            "-- Implement and verify rollback/undo strategy before applying.\n"
        )
        return path, content, "Flyway SQL 脚手架仅含注释；应用前必须完成迁移与回滚方案。"
    if framework == "prisma":
        migration_dir = directory / f"{timestamp}_{name}"
        path = migration_dir / "migration.sql"
        content = (
            f"-- Krelcode Prisma migration scaffold: {name}\n"
            "-- Replace this comment with project-native Prisma migration SQL.\n"
            "-- Do not apply until forward and rollback/recovery behavior is verified.\n"
        )
        return path, content, "Prisma SQL 脚手架仅含注释；优先使用 `prisma migrate dev` 生成正式迁移。"
    path = directory / f"{timestamp}_{name}.sql"
    content = (
        f"-- Krelcode generic migration scaffold: {name}\n"
        "-- Replace this comment with the forward migration SQL.\n"
        "-- Keep rollback instructions in a reviewed down/rollback artifact.\n"
    )
    return path, content, "通用 SQL 脚手架仅含注释；需按项目规范接入执行器与回滚流程。"


def _scaffold_directory(
    ctx: ToolContext, framework: str, raw_path: str,
    discovery: MigrationDiscovery,
) -> Path:
    if raw_path:
        return resolve_in_workspace(ctx, raw_path)
    candidates: dict[str, tuple[str, ...]] = {
        "alembic": ("alembic/versions", "migrations/versions"),
        "flyway": ("src/main/resources/db/migration", "db/migration", "migrations"),
        "prisma": ("prisma/migrations",),
        "generic": ("migrations", "db/migrations"),
    }
    for relative in candidates.get(framework, ()):
        path = discovery.root / relative
        if path.is_dir():
            return path
    raise ValueError(
        f"未找到 {framework} 迁移目录；请显式提供 path")


def _python_files(directory: Path) -> Tuple[Path, ...]:
    if not directory.is_dir():
        return ()
    return tuple(sorted(
        path for path in directory.iterdir()
        if path.is_file() and path.suffix == ".py" and not path.is_symlink()))


def _read_bounded(path: Path, max_bytes: int) -> Optional[str]:
    try:
        text, snapshot = read_text_file_with_snapshot(path)
    except (OSError, ValueError, UnsafeFileError, FileChangedError):
        return None
    if snapshot.size > max_bytes:
        return None
    return text


def _suggest_state_commands(frameworks: Sequence[str]) -> Tuple[str, ...]:
    commands: list[str] = []
    mapping = {
        "alembic": "alembic current",
        "django": "python manage.py showmigrations",
        "prisma": "npx prisma migrate status",
        "flyway": "flyway info",
        "liquibase": "liquibase status",
        "rails": "bin/rails db:migrate:status",
    }
    for framework in frameworks:
        command = mapping.get(framework)
        if command and command not in commands:
            commands.append(command)
    return tuple(commands)


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return path.name


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


__all__ = [
    "MigrationCreateTool",
    "MigrationInspectTool",
    "MigrationOptions",
    "MigrationValidateTool",
    "build_migration_tools",
]
