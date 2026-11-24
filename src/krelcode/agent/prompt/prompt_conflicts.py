"""Deterministic, content-light conflict analysis for assembled PromptFragments.

The analyzer intentionally does not call a model.  It works on a bounded set
of instruction sentences and emits only provenance, classifications, hashes
and counts.  Fragment text is never stored in a report, so the result can be
persisted in Context checkpoints and exposed through JSONL/stream-json.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable, Mapping, Optional, Sequence, Tuple

from krelcode.agent.prompt.fragments import (
    LAYER_L0_MANAGED_POLICY,
    LAYER_L1_BASE_AGENT,
    LAYER_L2_USER_GLOBAL,
    LAYER_L3_PROJECT_RULES,
    LAYER_L4_AGENT_STYLE,
    LAYER_L5_MODE_CONTRACT,
    LAYER_L9_WORKING,
    LAYER_L12_REMINDER,
    PromptFragment,
)


PROMPT_CONFLICT_VERSION = "1"
_MAX_SOURCE_CHARS = 512_000
_MAX_SENTENCES = 5_000
_MAX_SENTENCE_CHARS = 4_000

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"
STATUS_RESOLVED = "resolved"
STATUS_AMBIGUOUS = "ambiguous"

_INSTRUCTION_LAYERS = frozenset({
    LAYER_L0_MANAGED_POLICY,
    LAYER_L1_BASE_AGENT,
    LAYER_L2_USER_GLOBAL,
    LAYER_L3_PROJECT_RULES,
    LAYER_L4_AGENT_STYLE,
    LAYER_L5_MODE_CONTRACT,
    LAYER_L9_WORKING,
    LAYER_L12_REMINDER,
})

_CJK = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
_CJK_RE = re.compile(rf"^[{_CJK}]+$")
_TOKEN_RE = re.compile(rf"[a-z0-9_./:+#-]+|[{_CJK}]+", re.I)
_NEGATIVE_RE = re.compile(
    r"\b(?:do\s+not|don't|never|must\s+not|should\s+not|avoid|forbid|"
    r"disallow|disable|without|no)\b|"
    r"(?:不要|不得|禁止|避免|不可|不能|不允许|不应)",
    re.I,
)
_POSITIVE_RE = re.compile(
    r"\b(?:always|must|should|prefer|use|enable|allow|require|required|"
    r"only)\b|(?:必须|应该|使用|优先|启用|允许|需要|只允许)",
    re.I,
)
_CONDITIONAL_RE = re.compile(
    r"\b(?:if|when|unless|except|only\s+when|for\s+files?|in\s+directories?)\b|"
    r"(?:如果|当|除非|仅在|只在|对于)",
    re.I,
)
_IMPERATIVE_RE = re.compile(
    r"^\s*(?:run|read|write|edit|check|verify|format|test|add|remove|delete|"
    r"create|update|install|open|close|keep|make|ensure|return|commit|push|"
    r"inspect|search|find|review|fix|refactor|deploy|运行|读取|写入|修改|"
    r"检查|验证|格式化|添加|删除|创建|更新|安装|保持|确保|返回|提交|推送|"
    r"查看|搜索|查找|修复|重构|部署)\b",
    re.I,
)
_DIRECTIVE_PREFIX_RE = re.compile(
    r"^\s*(?:(?:please|always|never|must|should|do|do\s+not|don't|"
    r"prefer|avoid|use|enable|allow|require|only)\s+|"
    r"(?:请|务必|始终|总是|一律|必须|应该|使用|优先|启用|允许|需要|"
    r"不要|不得|禁止|避免|不可|不能|不允许|不应))",
    re.I,
)
_BULLET_RE = re.compile(r"^\s*(?:[-*+#]|\d+[.)])\s*")
_STOPWORDS = frozenset({
    "a", "an", "the", "to", "for", "in", "on", "with", "and", "or", "of",
    "is", "are", "be", "as", "at", "by", "from", "this", "that", "it",
    "please", "must", "should", "always", "never", "do", "not", "prefer",
    "use", "using", "enable", "allow", "require", "required", "only",
    "请", "务必", "始终", "总是", "一律", "应该", "必须", "使用", "优先",
    "启用", "允许", "需要", "不要", "不得", "禁止", "避免", "不可",
    "不能", "不允许", "不应", "以及", "并且", "然后",
})
_OPPOSITE_GROUPS: Tuple[
    tuple[frozenset[str], frozenset[str]], ...
] = (
    (frozenset(("tabs", "tab", "制表符")), frozenset(("spaces", "space", "空格"))),
    (frozenset(("concise", "brief", "简洁", "简短")),
     frozenset(("verbose", "detailed", "冗长", "详细"))),
    (frozenset(("strict", "严格")), frozenset(("lenient", "宽松"))),
    (frozenset(("sync", "同步")), frozenset(("async", "异步"))),
    (frozenset(("readonly", "read-only", "只读")),
     frozenset(("readwrite", "read-write", "读写"))),
    (frozenset(("include", "包含")), frozenset(("exclude", "排除"))),
    (frozenset(("before", "之前")), frozenset(("after", "之后"))),
    (frozenset(("enabled", "enable", "开启")),
     frozenset(("disabled", "disable", "关闭"))),
    (frozenset(("allow", "允许")), frozenset(("deny", "禁止"))),
    (frozenset(("on", "开")), frozenset(("off", "关"))),
)


def _hash(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _text(value: object, limit: int = 256) -> str:
    result = str(value or "").strip()
    return result if len(result) <= limit else result[:limit - 1] + "…"


def _int(value: object) -> int:
    if not isinstance(value, (int, float, str, bytes, bytearray)):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _sentences(text: str) -> Tuple[str, ...]:
    return _sentences_cached(str(text or "")[:_MAX_SOURCE_CHARS])


@lru_cache(maxsize=512)
def _sentences_cached(text: str) -> Tuple[str, ...]:
    """Split instructions into bounded sentences/lines without retaining code."""
    chunks: list[str] = []
    in_fence = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not line:
            continue
        line = _BULLET_RE.sub("", line)
        parts = re.split(
            rf"(?<=[.!?;。！？；])(?:\s+|(?=[{_CJK}]))",
            line,
        )
        for part in parts:
            value = " ".join(part.split()).strip()[:_MAX_SENTENCE_CHARS]
            if value:
                chunks.append(value)
                if len(chunks) >= _MAX_SENTENCES:
                    return tuple(chunks)
    return tuple(chunks)


def _tokens(text: str) -> Tuple[str, ...]:
    values: list[str] = []
    for match in _TOKEN_RE.findall(text):
        token = match.casefold()
        if token in _STOPWORDS:
            continue
        values.append(token)
        if _CJK_RE.match(token) and len(token) > 2:
            # A Chinese sentence does not provide whitespace-delimited words.
            # Character bigrams give the deterministic similarity/opposition
            # analyzer useful overlap without a language model tokenizer.
            values.extend(
                token[index:index + 2]
                for index in range(len(token) - 1)
                if token[index:index + 2] not in _STOPWORDS
            )
    return tuple(dict.fromkeys(values))


@lru_cache(maxsize=4096)
def _polarity(text: str) -> int:
    if _NEGATIVE_RE.search(text):
        return -1
    if _POSITIVE_RE.search(text):
        return 1
    if _IMPERATIVE_RE.search(text):
        return 1
    return 0


@lru_cache(maxsize=4096)
def _subject_tokens(text: str) -> Tuple[str, ...]:
    # Remove leading modals and polarity markers.  The remaining tokens are a
    # compact, language-agnostic approximation of the directive's subject.
    normalized = _DIRECTIVE_PREFIX_RE.sub("", text, count=1)
    normalized = _NEGATIVE_RE.sub(" ", normalized)
    normalized = _POSITIVE_RE.sub(" ", normalized)
    return _tokens(normalized)


def _jaccard(first: Iterable[str], second: Iterable[str]) -> float:
    left, right = set(first), set(second)
    if not left and not right:
        return 1.0
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _similar(first: Tuple[str, ...], second: Tuple[str, ...]) -> float:
    if first == second:
        return 1.0
    if not first or not second:
        return 0.0
    overlap = _jaccard(first, second)
    # A one-token command name or file/package name is a meaningful subject.
    if len(first) == len(second) == 1 and first == second:
        return 1.0
    return overlap


@lru_cache(maxsize=8192)
def _opposing_values(
    first: Tuple[str, ...], second: Tuple[str, ...],
) -> bool:
    left, right = frozenset(first), frozenset(second)
    for left_values, right_values in _OPPOSITE_GROUPS:
        left_uses_left = _contains_value(left, left_values)
        left_uses_right = _contains_value(left, right_values)
        right_uses_left = _contains_value(right, left_values)
        right_uses_right = _contains_value(right, right_values)
        if left_uses_left and left_uses_right:
            continue
        if right_uses_left and right_uses_right:
            continue
        uses_opposite_sides = (
            (left_uses_left and right_uses_right)
            or (left_uses_right and right_uses_left)
        )
        if not uses_opposite_sides:
            continue
        shared_context = {
            token for token in left & right
            if not _contains_value(frozenset((token,)), left_values)
            and not _contains_value(frozenset((token,)), right_values)
        }
        if shared_context:
            return True
        residual_left = left - left_values - right_values
        residual_right = right - left_values - right_values
        if residual_left == residual_right or _jaccard(
                residual_left, residual_right) >= 0.6:
            return True
    return False


@lru_cache(maxsize=8192)
def _contains_value(tokens: frozenset[str], values: frozenset[str]) -> bool:
    for token in tokens:
        if token in values:
            return True
        if not _CJK_RE.match(token):
            continue
        if any(
            _CJK_RE.match(value)
            and (value in token or token in value)
            for value in values
        ):
            return True
    return False


def _layer_index(layer: str) -> int:
    order = (
        LAYER_L0_MANAGED_POLICY,
        LAYER_L1_BASE_AGENT,
        LAYER_L2_USER_GLOBAL,
        LAYER_L3_PROJECT_RULES,
        LAYER_L4_AGENT_STYLE,
        LAYER_L5_MODE_CONTRACT,
        LAYER_L9_WORKING,
        LAYER_L12_REMINDER,
    )
    try:
        return order.index(layer)
    except ValueError:
        return len(order)


def _authority_key(fragment: PromptFragment) -> tuple[int, int, int, str]:
    """Return the deterministic authority ordering used for resolution."""
    metadata = fragment.metadata or {}
    rule_scope = str(metadata.get("rule_scope", "") or "").casefold()
    precedence = _int(metadata.get("rule_precedence"))
    if fragment.layer == LAYER_L0_MANAGED_POLICY or rule_scope == "managed":
        tier = 0
    elif fragment.layer == LAYER_L5_MODE_CONTRACT:
        tier = 1
    elif fragment.layer == LAYER_L1_BASE_AGENT:
        tier = 2
    elif rule_scope == "directory":
        tier = 3
    elif rule_scope == "project" or fragment.layer == LAYER_L3_PROJECT_RULES:
        tier = 4
    elif rule_scope == "user" or fragment.layer == LAYER_L2_USER_GLOBAL:
        tier = 5
    elif fragment.layer == LAYER_L4_AGENT_STYLE:
        tier = 6
    elif fragment.layer == LAYER_L9_WORKING:
        tier = 7
    elif fragment.layer == LAYER_L12_REMINDER:
        tier = 8
    else:
        tier = 10
    # Fragment ID is deliberately not part of the authority key: two rules at
    # the same scope and layer with equal precedence are ambiguous, not ordered
    # by registration/name.
    return (tier, precedence, _layer_index(fragment.layer), "")


def _instruction_fragment(fragment: PromptFragment) -> bool:
    metadata = fragment.metadata or {}
    if metadata.get("instruction") is False:
        return False
    if str(metadata.get("source_role", "") or "") == "evidence":
        return False
    return bool(fragment.content.strip()) and (
        fragment.layer in _INSTRUCTION_LAYERS
        or bool(metadata.get("instruction"))
        or bool(metadata.get("rule_scope"))
    )


@dataclass(frozen=True)
class PromptConflict:
    """One content-free conflict record between two instruction fragments."""

    id: str
    code: str
    severity: str
    status: str
    resolution: str
    confidence: int
    left_fragment_id: str
    right_fragment_id: str
    left_source: str
    right_source: str
    left_layer: str
    right_layer: str
    left_scope: str
    right_scope: str
    winner_fragment_id: str = ""
    topic_hash: str = ""
    left_directive_hash: str = ""
    right_directive_hash: str = ""
    left_token_count: int = 0
    right_token_count: int = 0
    token_overlap: int = 0

    @property
    def unresolved(self) -> bool:
        return self.status == STATUS_AMBIGUOUS

    def as_record(self) -> dict[str, object]:
        return {
            "id": self.id,
            "code": self.code,
            "severity": self.severity,
            "status": self.status,
            "resolution": self.resolution,
            "confidence": self.confidence,
            "left_fragment_id": self.left_fragment_id,
            "right_fragment_id": self.right_fragment_id,
            "left_source": self.left_source,
            "right_source": self.right_source,
            "left_layer": self.left_layer,
            "right_layer": self.right_layer,
            "left_scope": self.left_scope,
            "right_scope": self.right_scope,
            "winner_fragment_id": self.winner_fragment_id,
            "topic_hash": self.topic_hash,
            "left_directive_hash": self.left_directive_hash,
            "right_directive_hash": self.right_directive_hash,
            "left_token_count": self.left_token_count,
            "right_token_count": self.right_token_count,
            "token_overlap": self.token_overlap,
            "unresolved": self.unresolved,
        }

    @classmethod
    def from_record(cls, raw: Mapping[str, object]) -> "PromptConflict":
        return cls(
            id=_text(raw.get("id")),
            code=_text(raw.get("code")),
            severity=_text(raw.get("severity")) or SEVERITY_WARNING,
            status=_text(raw.get("status")) or STATUS_AMBIGUOUS,
            resolution=_text(raw.get("resolution")),
            confidence=min(100, _int(raw.get("confidence"))),
            left_fragment_id=_text(raw.get("left_fragment_id")),
            right_fragment_id=_text(raw.get("right_fragment_id")),
            left_source=_text(raw.get("left_source")),
            right_source=_text(raw.get("right_source")),
            left_layer=_text(raw.get("left_layer")),
            right_layer=_text(raw.get("right_layer")),
            left_scope=_text(raw.get("left_scope")),
            right_scope=_text(raw.get("right_scope")),
            winner_fragment_id=_text(raw.get("winner_fragment_id")),
            topic_hash=_text(raw.get("topic_hash"), 128),
            left_directive_hash=_text(raw.get("left_directive_hash"), 128),
            right_directive_hash=_text(raw.get("right_directive_hash"), 128),
            left_token_count=_int(raw.get("left_token_count")),
            right_token_count=_int(raw.get("right_token_count")),
            token_overlap=_int(raw.get("token_overlap")),
        )


@dataclass(frozen=True)
class PromptConflictReport:
    """Bounded conflict-analysis result safe for logs and checkpoints."""

    conflicts: Tuple[PromptConflict, ...] = ()
    fragment_count: int = 0
    analyzed_fragment_count: int = 0
    directive_count: int = 0
    truncated: bool = False
    version: str = PROMPT_CONFLICT_VERSION

    @property
    def conflict_count(self) -> int:
        return len(self.conflicts)

    @property
    def unresolved_count(self) -> int:
        return sum(item.unresolved for item in self.conflicts)

    @property
    def error_count(self) -> int:
        return sum(item.severity == SEVERITY_ERROR for item in self.conflicts)

    @property
    def warning_count(self) -> int:
        return sum(item.severity == SEVERITY_WARNING for item in self.conflicts)

    @property
    def report_hash(self) -> str:
        return _hash(self.as_dict())

    def as_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "fragment_count": self.fragment_count,
            "analyzed_fragment_count": self.analyzed_fragment_count,
            "directive_count": self.directive_count,
            "truncated": self.truncated,
            "conflict_count": self.conflict_count,
            "unresolved_count": self.unresolved_count,
            "conflicts": [item.as_record() for item in self.conflicts],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object] | None) -> "PromptConflictReport":
        if not isinstance(raw, Mapping):
            return cls()
        values = raw.get("conflicts")
        candidates = (
            values if isinstance(values, (list, tuple)) else ())
        records = tuple(
            PromptConflict.from_record(item)
            for item in candidates[:500]
            if isinstance(item, Mapping)
        )
        return cls(
            conflicts=records,
            fragment_count=_int(raw.get("fragment_count")),
            analyzed_fragment_count=_int(raw.get("analyzed_fragment_count")),
            directive_count=_int(raw.get("directive_count")),
            truncated=(
                bool(raw.get("truncated", False))
                or len(candidates) > 500
            ),
            version=_text(raw.get("version")) or PROMPT_CONFLICT_VERSION,
        )


@dataclass(frozen=True)
class _Directive:
    fragment: PromptFragment
    sentence: str
    subject: Tuple[str, ...]
    polarity: int
    conditional: bool

    @property
    def digest(self) -> str:
        return _hash({
            "subject": self.subject,
            "polarity": self.polarity,
            "sentence": self.sentence.casefold(),
        })


class PromptConflictAnalyzer:
    """Detect clear directive contradictions without a model call."""

    def __init__(
        self,
        *,
        min_similarity: float = 0.72,
        max_conflicts: int = 100,
        max_directives: int = 2000,
        max_comparisons: int = 100_000,
    ) -> None:
        self.min_similarity = max(0.4, min(float(min_similarity), 1.0))
        self.max_conflicts = max(1, min(int(max_conflicts), 500))
        self.max_directives = max(1, min(int(max_directives), 5000))
        self.max_comparisons = max(
            1, min(int(max_comparisons), 500_000))

    def analyze(self, fragments: Sequence[PromptFragment]) -> PromptConflictReport:
        fragments = tuple(fragments)
        directives: list[_Directive] = []
        analyzed_fragments = 0
        truncated = False
        for fragment in fragments:
            if not _instruction_fragment(fragment):
                continue
            analyzed_fragments += 1
            if len(fragment.content) > _MAX_SOURCE_CHARS:
                truncated = True
            sentences = _sentences(fragment.content)
            if len(sentences) >= _MAX_SENTENCES:
                truncated = True
            for sentence in sentences:
                if len(directives) >= self.max_directives:
                    truncated = True
                    break
                subject = _subject_tokens(sentence)
                polarity = _polarity(sentence)
                if not subject or (polarity == 0 and len(subject) < 2):
                    continue
                directives.append(_Directive(
                    fragment=fragment,
                    sentence=sentence,
                    subject=subject,
                    polarity=polarity,
                    conditional=bool(_CONDITIONAL_RE.search(sentence)),
                ))
            if len(directives) >= self.max_directives:
                break

        candidate_pairs, comparisons_truncated = self._candidate_pairs(directives)
        truncated = truncated or comparisons_truncated
        conflicts: list[PromptConflict] = []
        seen: set[tuple[str, str, str, str]] = set()
        for left_index, right_index in candidate_pairs:
            candidate = self._compare(
                directives[left_index], directives[right_index])
            if candidate is None:
                continue
            key = (
                candidate.left_fragment_id,
                candidate.right_fragment_id,
                candidate.code,
                candidate.topic_hash,
            )
            if key in seen:
                continue
            seen.add(key)
            conflicts.append(candidate)

        conflicts.sort(key=lambda item: (
            item.severity != SEVERITY_ERROR,
            -item.confidence,
            item.left_fragment_id,
            item.right_fragment_id,
            item.id,
        ))
        if len(conflicts) > self.max_conflicts:
            conflicts = conflicts[:self.max_conflicts]
            truncated = True
        return PromptConflictReport(
            conflicts=tuple(conflicts),
            fragment_count=len(fragments),
            analyzed_fragment_count=analyzed_fragments,
            directive_count=len(directives),
            truncated=truncated,
        )

    def _candidate_pairs(
        self,
        directives: Sequence[_Directive],
    ) -> tuple[tuple[tuple[int, int], ...], bool]:
        """Index direct negations by shared topics and explicit value contrasts."""
        buckets: dict[str, list[int]] = {}
        for index, directive in enumerate(directives):
            for token in directive.subject:
                buckets.setdefault(f"token:{token}", []).append(index)

        pairs: set[tuple[int, int]] = set()
        truncated = False
        # Rare/specific buckets go first so a common word cannot consume the
        # comparison budget before a more relevant signal is examined.
        ordered = sorted(buckets.items(), key=lambda item: (len(item[1]), item[0]))
        for _, members in ordered:
            if len(members) < 2:
                continue
            negative = [
                index for index in members
                if directives[index].polarity < 0
            ]
            affirmative = [
                index for index in members
                if directives[index].polarity >= 0
            ]
            for first, second in itertools.product(negative, affirmative):
                pair = (first, second) if first < second else (second, first)
                if pair in pairs:
                    continue
                if len(pairs) >= self.max_comparisons:
                    truncated = True
                    break
                pairs.add(pair)
            if truncated:
                break

        if not truncated:
            for left_values, right_values in _OPPOSITE_GROUPS:
                left_side: list[int] = []
                right_side: list[int] = []
                for index, directive in enumerate(directives):
                    subject = frozenset(directive.subject)
                    left_match = _contains_value(subject, left_values)
                    right_match = _contains_value(subject, right_values)
                    if left_match == right_match:
                        continue
                    (left_side if left_match else right_side).append(index)
                for first, second in itertools.product(left_side, right_side):
                    pair = (first, second) if first < second else (second, first)
                    if pair in pairs:
                        continue
                    if len(pairs) >= self.max_comparisons:
                        truncated = True
                        break
                    pairs.add(pair)
                if truncated:
                    break
        return tuple(sorted(pairs)), truncated

    def _compare(
        self, left: _Directive, right: _Directive,
    ) -> Optional[PromptConflict]:
        if left.fragment.id == right.fragment.id and left.sentence == right.sentence:
            return None
        similarity = _similar(left.subject, right.subject)
        code = ""
        confidence = 0
        if (
            left.polarity
            and right.polarity
            and left.polarity != right.polarity
            and similarity >= self.min_similarity
        ):
            code = "direct_negation"
            confidence = int(96 if similarity >= 0.99 else 78)
        elif _opposing_values(left.subject, right.subject):
            # Opposing values intentionally tolerate low whole-sentence overlap
            # (for example "use tabs for indentation" vs "use spaces for
            # indentation"); residual context equality was checked by the
            # opposition detector.
            code = "opposing_value"
            confidence = 90 if similarity >= 0.99 else 76
        if not code:
            return None

        if left.conditional != right.conditional:
            return None
        if left.conditional or right.conditional:
            code = f"conditional_{code}"
            confidence = max(50, confidence - 14)

        left_key = _authority_key(left.fragment)
        right_key = _authority_key(right.fragment)
        if left_key < right_key:
            winner = left.fragment.id
            status = STATUS_RESOLVED
            resolution = self._resolution(left.fragment, right.fragment, left_key, right_key)
        elif right_key < left_key:
            winner = right.fragment.id
            status = STATUS_RESOLVED
            resolution = self._resolution(right.fragment, left.fragment, right_key, left_key)
        else:
            winner = ""
            status = STATUS_AMBIGUOUS
            resolution = "equal_authority"

        severity = (
            SEVERITY_INFO
            if left.conditional or right.conditional
            else SEVERITY_WARNING
            if status == STATUS_RESOLVED
            else SEVERITY_ERROR
        )
        left_first = (left.fragment.id, left.sentence) <= (
            right.fragment.id, right.sentence)
        if not left_first:
            left, right = right, left
        topic = tuple(sorted(set(left.subject) | set(right.subject)))
        topic_hash = _hash(topic)
        conflict_id = _hash({
            "version": PROMPT_CONFLICT_VERSION,
            "code": code,
            "left": left.fragment.id,
            "right": right.fragment.id,
            "topic": topic_hash,
            "left_directive": left.digest,
            "right_directive": right.digest,
        })[:20]
        return PromptConflict(
            id=conflict_id,
            code=code,
            severity=severity,
            status=status,
            resolution=resolution,
            confidence=min(100, confidence),
            left_fragment_id=left.fragment.id,
            right_fragment_id=right.fragment.id,
            left_source=left.fragment.source,
            right_source=right.fragment.source,
            left_layer=left.fragment.layer,
            right_layer=right.fragment.layer,
            left_scope=left.fragment.scope,
            right_scope=right.fragment.scope,
            winner_fragment_id=winner,
            topic_hash=topic_hash,
            left_directive_hash=left.digest,
            right_directive_hash=right.digest,
            left_token_count=len(left.subject),
            right_token_count=len(right.subject),
            token_overlap=len(set(left.subject) & set(right.subject)),
        )

    @staticmethod
    def _resolution(
        winner: PromptFragment,
        loser: PromptFragment,
        winner_key: tuple[int, int, int, str],
        loser_key: tuple[int, int, int, str],
    ) -> str:
        if winner_key[0] == 0:
            return "managed_policy"
        if winner_key[2] != loser_key[2]:
            return "layer_precedence"
        if winner_key[1] != loser_key[1]:
            return "rule_precedence"
        return "authority_precedence"


__all__ = [
    "PROMPT_CONFLICT_VERSION",
    "PromptConflict",
    "PromptConflictAnalyzer",
    "PromptConflictReport",
    "SEVERITY_ERROR",
    "SEVERITY_INFO",
    "SEVERITY_WARNING",
    "STATUS_AMBIGUOUS",
    "STATUS_RESOLVED",
]
