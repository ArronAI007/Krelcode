"""Slash-command handlers for scoped memory, migration and Auto Memory review."""

from __future__ import annotations

import shlex
from typing import Iterable, Optional, Sequence, cast

from ....contracts.application import RiskLevel
from ....contracts.change_admission import (
    AdmissionDecision,
    ChangeApproval,
    ChangeCandidate,
    ChangeType,
    EvaluationCheck,
    EvaluationReport,
    RollbackManifest,
)
from ....contracts.tool import ToolContext
from ....core.tokens import estimate_text_tokens
from ....core.events import AgentEvent, MemoryCandidateObserved, MemoryWritten
from ....session.memory import MemoryStore, sync_memory_fragment
from ....session.memory_candidates import (
    TRIGGER_LABELS,
    AutoMemoryCoordinator,
    AutoMemoryMode,
    MemoryCandidate,
    MemoryCandidateQueue,
    MemoryCandidateStatus,
    MemoryCandidateTrigger,
)
from ....session.memory_dream import (
    DreamKind,
    DreamStatus,
    MemoryDreamCoordinator,
)
from ....session.memory_models import (
    SCOPE_LABELS,
    MemoryEntry,
    MemoryScope,
    coerce_memory_scope,
    coerce_memory_type,
)
from ....governance.change_pipeline import DefaultAdmissionGate
from ....governance.control_plane.helpers import governance_for_change
from ..context import CommandContext
from ..types import CommandResult


def _ok(*output: str) -> CommandResult:
    return CommandResult(handled=True, output=tuple(output))


def _parts(raw: str) -> list[str]:
    try:
        return shlex.split(raw) if raw.strip() else []
    except ValueError:
        return raw.split()


def _resolve_candidate(
    queue: MemoryCandidateQueue, token: str
) -> Optional[MemoryCandidate]:
    candidate = queue.get(token)
    if candidate is not None:
        return candidate
    matches = [item for item in queue.list(limit=0) if item.id.endswith(token)]
    return matches[-1] if len(matches) == 1 else None


def _resolve_entry(store: MemoryStore, token: str) -> MemoryEntry | None:
    entries = store.list(limit=0)
    target = str(token or "").strip()
    if not target:
        return None
    exact = next((entry for entry in entries if entry.id == target), None)
    if exact is not None:
        return exact
    matches = [entry for entry in entries if entry.id.endswith(target)]
    return matches[-1] if len(matches) == 1 else None


def _candidate_lines(candidates: Sequence[MemoryCandidate]) -> list[str]:
    lines = [f"  [memory review] {len(candidates)} 个候选"]
    for item in reversed(candidates):
        trigger = TRIGGER_LABELS.get(
            cast(MemoryCandidateTrigger, item.trigger),
            item.trigger_value)
        conflict = (
            f" · 冲突 {len(item.conflict_ids)}" if item.conflict_ids else "")
        lines.append(
            f"  {item.id} · {item.status_value} · 置信度 {item.confidence:.2f}"
            f" · {item.memory_type_value} · {trigger}"
            f" · 证据 {len(item.evidence)}{conflict}"
            + ("" if item.structurally_complete else " · 待补 Why/How"))
        lines.append(f"    {item.text}")
    if candidates:
        lines.append(
            "  /memory review accept <id> 接受；/memory review reject <id> 拒绝")
    return lines


def _admit_memory_change(
    *,
    change_type: ChangeType,
    target: str,
    before: dict[str, object],
    after: dict[str, object],
    risk: RiskLevel,
    tool_context: Optional[ToolContext],
) -> AdmissionDecision:
    candidate = ChangeCandidate(
        change_type=change_type,
        target=target,
        before=before,
        after=after,
        risk=risk,
        affected_artifacts=({"kind": "memory_store"},),
    )
    return DefaultAdmissionGate().admit(
        candidate,
        evaluation=EvaluationReport.from_checks(
            candidate.candidate_id,
            [EvaluationCheck(
                name="memory_change_requested",
                passed=True,
                reason="explicit user memory command",
                score=1.0,
            )],
        ),
        approval=ChangeApproval(
            approved=True,
            actor="session-user",
            reason="用户显式执行 Memory 变更命令",
            scope="memory",
        ),
        rollback=RollbackManifest(
            candidate_id=candidate.candidate_id,
            snapshot=before,
            instructions="从迁移备份或 AutoDream 前记忆快照恢复",
        ),
        audit={"entrypoint": "/memory"},
        governance_decision=governance_for_change(
            candidate, tool_context=tool_context),
    )


def _entry_lines(entries: Iterable[MemoryEntry]) -> list[str]:
    lines: list[str] = []
    for entry in reversed(tuple(entries)):
        scope = coerce_memory_scope(entry.scope)
        pinned = " · pinned" if entry.pinned else ""
        lines.append(
            f"  {entry.id} · {scope.value}/{entry.kind_value}"
            f" · type={entry.memory_type_value}{pinned}"
            f" · {entry.text}")
    return lines


async def _emit(ctx: CommandContext, event: AgentEvent) -> None:
    bus = ctx.loop.services.core.bus
    if bus is not None:
        await bus.emit(event)


async def _written(
    ctx: CommandContext,
    *,
    memory_id: str,
    action: str,
    scope: str = "",
    kind: str = "",
    tokens: int = 0,
    source: str = "manual",
) -> None:
    await _emit(ctx, MemoryWritten(
        memory_id=memory_id,
        action=action,
        scope=scope,
        kind=kind,
        tokens=max(0, int(tokens)),
        source=source,
    ))


def _parse_add(rest: str) -> tuple[str, MemoryScope, dict[str, str]]:
    tokens = _parts(rest)
    if not tokens:
        raise ValueError
    scope_value = ""
    text_tokens: list[str] = []
    options = {"memory_type": "", "why": "", "how": "", "topic": ""}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--scope" and index + 1 < len(tokens):
            scope_value = tokens[index + 1]
            index += 2
            continue
        if token.startswith("--scope="):
            scope_value = token.split("=", 1)[1]
            index += 1
            continue
        option_aliases = {
            "--type": "memory_type",
            "--why": "why",
            "--how": "how",
            "--topic": "topic",
        }
        if token in option_aliases and index + 1 < len(tokens):
            options[option_aliases[token]] = tokens[index + 1]
            index += 2
            continue
        matched_option = next(
            (name for name in option_aliases if token.startswith(name + "=")),
            "",
        )
        if matched_option:
            options[option_aliases[matched_option]] = token.split("=", 1)[1]
            index += 1
            continue
        text_tokens.append(token)
        index += 1
    if scope_value:
        scope = coerce_memory_scope(scope_value)
        text = " ".join(text_tokens).strip()
    else:
        # Compatibility: /memory add <scope> <text>
        try:
            legacy_scope = coerce_memory_scope(tokens[0])
        except ValueError:
            scope = coerce_memory_scope("project")
            text = " ".join(tokens).strip()
        else:
            scope = legacy_scope
            text = " ".join(tokens[1:]).strip()
    if not text:
        raise ValueError
    return text, scope, options


async def handle_memory(ctx: CommandContext) -> CommandResult:
    store = cast(Optional[MemoryStore], ctx.loop.services.session.memory)
    if store is None:
        return _ok("  （Memory 未启用）")
    auto_memory = cast(
        Optional[AutoMemoryCoordinator],
        ctx.loop.services.extensions.auto_memory,
    )
    raw = ctx.request.arguments.strip()
    operation, _, rest = raw.partition(" ")
    operation = operation.lower() or "list"
    rest = rest.strip()

    if operation == "list":
        tokens = _parts(rest)
        if len(tokens) > 1:
            return _ok("  用法: /memory list [user|project|session]")
        scopes = None
        label = "全部"
        if tokens:
            try:
                scope = coerce_memory_scope(tokens[0])
            except ValueError as exc:
                return _ok(f"  [memory] {exc}")
            scopes = (scope,)
            label = SCOPE_LABELS[scope]
        entries = store.list(limit=50, scopes=scopes)
        if not entries:
            lines = [
                f"  [memory] {label}作用域当前没有已确认记忆",
                "  #内容 添加项目记忆；#user: / #project: / #session: 指定作用域",
            ]
            if auto_memory is not None:
                pending = auto_memory.queue.pending_count()
                if pending:
                    lines.append(
                        f"  有 {pending} 条 Auto Memory 候选待审核"
                        "（/memory review）")
            return _ok(*lines)
        lines = [f"  [memory] {len(entries)} 条{label}记忆（最新优先）"]
        lines.extend(_entry_lines(entries))
        if auto_memory is not None:
            pending = auto_memory.queue.pending_count()
            if pending:
                lines.append(
                    f"  待审核 Auto Memory 候选: {pending}（/memory review）")
        return _ok(*lines)

    if operation == "scopes":
        summary = store.scope_summary()
        lines = ["  [memory scopes]"]
        for scope_name in ("user", "project", "session"):
            item = summary[scope_name]
            lines.append(
                f"  {SCOPE_LABELS[coerce_memory_scope(scope_name)]} ({scope_name})"
                f" · {item['entries']} 条 · rev {item.get('revision', 0)}"
                f" · {item['path']}")
        if auto_memory is not None:
            lines.append(
                f"  Auto Memory · mode={auto_memory.mode.value}"
                f" · pending={auto_memory.queue.pending_count()}"
                f" · queue={auto_memory.queue.path}")
        lines.append(f"  Workspace index · {store.index_path}")
        lines.append(f"  Workspace topics · {store.topics_dir}")
        lines.append(f"  User index · {store.user_index_path}")
        lines.append(f"  User topics · {store.user_topics_dir}")
        return _ok(*lines)

    if operation == "show":
        token = rest.split(maxsplit=1)[0] if rest else ""
        entry = _resolve_entry(store, token)
        if entry is None:
            return _ok(
                f"  [memory] 未找到: {token}" if token
                else "  用法: /memory show <id>")
        return _ok(
            f"  [memory] {entry.id}",
            f"  作用域: {entry.scope_value} · 类型: {entry.memory_type_value}"
            f" · legacy kind: {entry.kind_value}"
            f" · source: {entry.source}"
            + (" · pinned" if entry.pinned else ""),
            f"  topic: {entry.topic or '(entry-specific)'}"
            f" · structured: {str(entry.is_structured).lower()}",
            f"  created: {entry.created_at} · updated: {entry.updated_at}",
            f"  tags: {', '.join(entry.tags) or '(none)'}"
            f" · tokens: {estimate_text_tokens(entry.text)}",
            *([f"  Why: {entry.why}"] if entry.why else []),
            *([f"  How to apply: {entry.how_to_apply}"]
              if entry.how_to_apply else []),
            "  内容:",
            entry.text,
        )

    if operation == "add":
        try:
            text, scope, options = _parse_add(rest)
            memory_type = coerce_memory_type(
                options.get("memory_type") or "reference")
            entry = store.add(
                text,
                scope=scope,
                memory_type=memory_type,
                topic=options.get("topic") or "",
                why=options.get("why") or "",
                how_to_apply=options.get("how") or "",
            )
        except (OSError, ValueError) as exc:
            return _ok(
                "  [memory] 写入失败: "
                + (str(exc) if str(exc) else
                   "用法: /memory add <text> [--scope user|project|session] "
                   "[--type user|feedback|project|reference] "
                   "[--why <原因>] [--how <应用方式>] [--topic <主题>]"))
        sync_memory_fragment(ctx.loop, store)
        await _written(
            ctx, memory_id=entry.id, action="added", scope=entry.scope_value,
            kind=entry.kind_value, tokens=estimate_text_tokens(entry.text))
        return _ok(
            f"  [memory] 已写入{SCOPE_LABELS[scope]}记忆 {entry.id}"
            f" · type={entry.memory_type_value}",
            "  feedback/project 必须同时提供 --why 和 --how；"
            "主题正文按需由 memory_search(id|topic) 读取")

    if operation in {"remove", "forget"}:
        entry_id = rest.split(maxsplit=1)[0] if rest else ""
        if not entry_id:
            return _ok(f"  用法: /memory {operation} <id>")
        try:
            removed = store.remove(entry_id)
        except OSError as exc:
            return _ok(f"  [memory] 删除失败: {exc}")
        if removed is None:
            return _ok(f"  [memory] 未找到: {entry_id}")
        sync_memory_fragment(ctx.loop, store)
        await _written(
            ctx, memory_id=removed.id, action="removed",
            scope=removed.scope_value, kind=removed.kind_value)
        return _ok(f"  [memory] 已删除: {removed.id}")

    if operation in {"pin", "unpin"}:
        entry_id = rest.split(maxsplit=1)[0] if rest else ""
        if not entry_id:
            return _ok(f"  用法: /memory {operation} <id>")
        try:
            if auto_memory is not None:
                entry = (
                    auto_memory.pin(entry_id)
                    if operation == "pin" else auto_memory.unpin(entry_id)
                )
            else:
                entry = (
                    store.pin(entry_id)
                    if operation == "pin" else store.unpin(entry_id)
                )
        except OSError as exc:
            return _ok(f"  [memory] 更新失败: {exc}")
        if entry is None:
            return _ok(f"  [memory] 未找到: {entry_id}")
        sync_memory_fragment(ctx.loop, store)
        state = "固定" if entry.pinned else "取消固定"
        await _written(
            ctx, memory_id=entry.id,
            action="pinned" if entry.pinned else "unpinned",
            scope=entry.scope_value, kind=entry.kind_value)
        return _ok(f"  [memory] 已{state}: {entry.id}")

    if operation == "search":
        query = rest.strip()
        if not query:
            return _ok("  用法: /memory search <关键词> [--scope <scope>]")
        tokens = _parts(query)
        scopes = None
        if "--scope" in tokens:
            index = tokens.index("--scope")
            if index + 1 >= len(tokens):
                return _ok("  用法: /memory search <关键词> [--scope <scope>]")
            try:
                scopes = (coerce_memory_scope(tokens[index + 1]),)
            except ValueError as exc:
                return _ok(f"  [memory] {exc}")
            tokens = tokens[:index] + tokens[index + 2:]
        query = " ".join(tokens).strip()
        if not query:
            return _ok("  用法: /memory search <关键词> [--scope <scope>]")
        entries = store.search(query, limit=10, scopes=scopes)
        if not entries:
            return _ok(f"  [memory] 没有匹配: {query}")
        lines = [f"  [memory search] {len(entries)} 条"]
        lines.extend(_entry_lines(entries))
        return _ok(*lines)

    if operation == "mode":
        if auto_memory is None:
            return _ok("  （Auto Memory 未启用）")
        if not rest:
            return _ok(
                f"  [memory] Auto Memory mode={auto_memory.mode.value}",
                "  可选: off | suggest | auto；默认 suggest，仅入队不自动写入",
                "  切换: /memory mode <off|suggest|auto>")
        try:
            mode = AutoMemoryMode(rest.split()[0].lower())
        except ValueError:
            return _ok("  用法: /memory mode <off|suggest|auto>")
        auto_memory.set_mode(mode)
        note = {
            AutoMemoryMode.OFF: "关闭候选提取",
            AutoMemoryMode.SUGGEST: "候选仅入队，等待 /memory review",
            AutoMemoryMode.AUTO: (
                f"置信度 ≥ {auto_memory.auto_threshold:.2f} 且无冲突时自动写入"),
        }[mode]
        return _ok(f"  [memory] Auto Memory mode={mode.value}（{note}）")

    if operation == "review":
        if auto_memory is None:
            return _ok("  （Auto Memory 未启用）")
        suboperation, _, review_rest = rest.partition(" ")
        suboperation = suboperation.lower() or "pending"
        status_map = {
            "pending": MemoryCandidateStatus.PENDING,
            "accepted": MemoryCandidateStatus.ACCEPTED,
            "rejected": MemoryCandidateStatus.REJECTED,
            "superseded": MemoryCandidateStatus.SUPERSEDED,
            "all": None,
        }
        if suboperation in status_map:
            candidates = auto_memory.queue.list(
                status=status_map[suboperation], limit=50)
            return _ok(*_candidate_lines(candidates))
        if suboperation not in {"accept", "reject"}:
            return _ok(
                "  用法: /memory review [pending|accepted|rejected|all] | "
                "/memory review accept|reject <id>")
        token = review_rest.strip().split(maxsplit=1)[0] if review_rest.strip() else ""
        if not token:
            return _ok(f"  用法: /memory review {suboperation} <id>")
        candidate = _resolve_candidate(auto_memory.queue, token)
        if candidate is None:
            return _ok(f"  [memory] 未找到候选: {token}")
        if suboperation == "accept":
            try:
                accepted = auto_memory.accept(candidate.id)
            except (OSError, ValueError) as exc:
                return _ok(f"  [memory] 接受失败: {exc}")
            if accepted is None:
                return _ok(f"  [memory] 未找到候选: {token}")
            sync_memory_fragment(ctx.loop, store)
            await _emit(ctx, MemoryCandidateObserved(
                candidate_id=accepted.id,
                status=accepted.status_value,
                trigger=accepted.trigger_value,
                confidence=accepted.confidence,
                conflict_count=len(accepted.conflict_ids),
                digest=accepted.digest,
            ))
            accepted_entry = _resolve_entry(
                store, accepted.existing_memory_id or "")
            await _written(
                ctx, memory_id=accepted.existing_memory_id or "",
                action="accepted",
                scope=accepted_entry.scope_value if accepted_entry else "project",
                kind=accepted_entry.kind_value if accepted_entry else "knowledge",
                source="auto-memory:review")
            return _ok(
                f"  [memory] 已接受 {accepted.id}"
                f" → {accepted.memory_type_value} {accepted.existing_memory_id}")
        try:
            rejected = auto_memory.reject(candidate.id)
        except OSError as exc:
            return _ok(f"  [memory] 拒绝失败: {exc}")
        if rejected is None:
            return _ok(f"  [memory] 未找到候选: {token}")
        await _emit(ctx, MemoryCandidateObserved(
            candidate_id=rejected.id,
            status=rejected.status_value,
            trigger=rejected.trigger_value,
            confidence=rejected.confidence,
            conflict_count=len(rejected.conflict_ids),
            digest=rejected.digest,
        ))
        return _ok(f"  [memory] 已拒绝 {rejected.id}（不会写入长期记忆）")

    if operation == "migrate":
        tokens = _parts(rest)
        if len(tokens) > 1:
            return _ok("  用法: /memory migrate [user|project|session]")
        migrate_scope: MemoryScope | str = MemoryScope.PROJECT
        if tokens:
            try:
                migrate_scope = coerce_memory_scope(tokens[0])
            except ValueError as exc:
                return _ok(f"  [memory] {exc}")
        admission = _admit_memory_change(
            change_type=ChangeType.MEMORY_MIGRATION,
            target=f"memory:{migrate_scope}",
            before={"scope": str(migrate_scope), "schema": "legacy"},
            after={"scope": str(migrate_scope), "schema": "memory-v2"},
            risk=RiskLevel.MEDIUM,
            tool_context=ctx.tool_context,
        )
        if not admission.admitted:
            return _ok(
                f"  [memory] 迁移未准入: {admission.status.value}"
                f" · {admission.reason}")
        try:
            report = store.migrate(migrate_scope)
        except OSError as exc:
            return _ok(f"  [memory] 迁移失败: {exc}")
        sync_memory_fragment(ctx.loop, store)
        await _written(
            ctx, memory_id="", action="migrated", scope=report.scope,
            tokens=0, source="migration")
        lines = [
            f"  [memory migrate] {report.scope} · {'已更新' if report.changed else '无需更新'}",
            f"  scanned {report.scanned} · kept {report.kept}"
            f" · migrated {report.migrated} · duplicates {report.duplicates}"
            f" · skipped {report.skipped}",
            f"  path: {report.path}",
        ]
        if report.backup_path:
            lines.append(f"  backup: {report.backup_path}")
        return _ok(*lines)

    if operation == "dream":
        dream = cast(
            Optional[MemoryDreamCoordinator],
            ctx.loop.memory_dream,
        )
        if dream is None:
            return _ok("  （AutoDream 未启用）")
        suboperation, _, dream_rest = rest.partition(" ")
        suboperation = suboperation.lower() or "review"
        if suboperation == "run":
            created = dream.run()
            return _ok(
                f"  [dream] 本次生成 {len(created)} 个新治理提案",
                f"  pending: {len(dream.dream_store.list(status=DreamStatus.PENDING, limit=0))}",
                "  使用 /memory dream review 查看，accept/reject 审核")
        if suboperation == "review":
            proposals = dream.dream_store.list(
                status=DreamStatus.PENDING, limit=50)
            lines = [f"  [dream review] {len(proposals)} 个待审核提案"]
            for proposal in proposals:
                lines.append(
                    f"  {proposal.id} · {DreamKind(proposal.kind).value}"
                    f" · confidence {proposal.confidence:.2f}"
                    f" · memories {len(proposal.memory_ids)}"
                    f" · {proposal.title}")
            return _ok(*lines)
        if suboperation not in {"accept", "reject"}:
            return _ok(
                "  用法: /memory dream [run|review|accept <id>|reject <id>]")
        token = dream_rest.strip().split(maxsplit=1)[0] if dream_rest.strip() else ""
        if not token:
            return _ok(f"  用法: /memory dream {suboperation} <id>")
        if suboperation == "accept":
            admission = _admit_memory_change(
                change_type=ChangeType.AUTO_DREAM,
                target=f"dream:{token}",
                before={"proposal": token, "status": "pending"},
                after={"proposal": token, "status": "accepted"},
                risk=RiskLevel.MEDIUM,
                tool_context=ctx.tool_context,
            )
            if not admission.admitted:
                return _ok(
                    f"  [dream] 合并未准入: {admission.status.value}"
                    f" · {admission.reason}")
        result = (
            dream.accept(token)
            if suboperation == "accept" else dream.reject(token)
        )
        if result is None:
            return _ok(f"  [dream] 未找到提案: {token}")
        return _ok(
            f"  [dream] {result.id} → {DreamStatus(result.status).value}",
            f"  {result.title}")

    return _ok(
        "  用法: /memory [list [scope]|scopes|show|add|remove|forget|"
        "pin|unpin|search|mode|review|migrate|dream]",
        "  add 支持 --type user|feedback|project|reference、--why、--how、--topic")
