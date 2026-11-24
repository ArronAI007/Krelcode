"""Normalization, sanitization and conservative conflict helpers."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Optional

from ..core.redact import SecretRedactor, redact_obvious_secrets
from .memory_models import MAX_ENTRY_CHARS, MAX_TAG_CHARS, MemoryEntry

def _redact_obvious_secrets(text: str) -> str:
    return redact_obvious_secrets(text)


def sanitize_memory_text(
    text: str,
    *,
    redactor: Optional[SecretRedactor] = None,
    max_chars: int = MAX_ENTRY_CHARS,
) -> str:
    """Normalize and redact text before it reaches memory persistence.

    This helper is shared by permanent entries, candidate queues and evidence
    excerpts so no write path can accidentally bypass the secret filter.
    """
    normalized = " ".join(str(text).split())
    if not normalized:
        raise ValueError("记忆内容不能为空")
    if len(normalized) > max_chars:
        normalized = normalized[:max_chars].rstrip()
    normalized = _redact_obvious_secrets(normalized)
    if redactor is not None:
        normalized = redactor.redact(normalized)
    return normalized


def normalize_memory_text(text: str) -> str:
    """Return a stable comparison form while preserving paths and CLI flags."""
    normalized = unicodedata.normalize("NFKC", str(text)).casefold()
    normalized = normalized.replace("“", '"').replace("”", '"')
    normalized = " ".join(normalized.split())
    normalized = re.sub(r"^[\-*#>\s]+", "", normalized)
    normalized = re.sub(r"[\s.,，。!！?？;；:：]+$", "", normalized)
    return normalized


def memory_digest(text: str) -> str:
    normalized = normalize_memory_text(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


_NEGATION_MARKERS = (
    " not ", " never ", " does not ", " do not ", " don't ", " isn't ",
    " doesn't ",
    "不使用", "不用", "不是", "不要", "禁止", "不能", "没有",
)
_ASSERTION_MARKERS = (
    "使用", "采用", "由", "负责", "位于", "包含", "支持",
    " uses ", " use ", " using ", " consists ", " contains ", " has ",
    " is ",
)
_TEST_COMMAND_RE = re.compile(
    r"(?i)\b(?:python(?:\d+(?:\.\d+)?)?\s+-m\s+)?"
    r"(?:pytest|py\.test|tox|nox|npm(?:\s+run)?\s+test|pnpm\s+test|"
    r"yarn\s+test|bun\s+test|cargo\s+test|go\s+test|dotnet\s+test|"
    r"mvn\s+test|gradlew?(?:\s+test)?|make\s+test|ruff\s+check)\b"
)


def extract_test_command(text: str) -> str:
    """Extract a recognizable verification command, if present."""
    match = _TEST_COMMAND_RE.search(str(text))
    if match is None:
        return ""
    return normalize_memory_text(match.group(0))


def memory_texts_conflict(first: str, second: str) -> bool:
    """Conservatively detect contradictory or competing stable knowledge."""
    left = normalize_memory_text(first)
    right = normalize_memory_text(second)
    if not left or not right or left == right:
        return False

    left_command = extract_test_command(left)
    right_command = extract_test_command(right)
    if left_command and right_command and left_command != right_command:
        return True

    padded_left = f" {left} "
    padded_right = f" {right} "
    left_negated = any(marker in padded_left for marker in _NEGATION_MARKERS)
    right_negated = any(marker in padded_right for marker in _NEGATION_MARKERS)
    if left_negated == right_negated:
        return False
    left_core = _conflict_core(left)
    right_core = _conflict_core(right)
    if left_core and right_core:
        if left_core in right_core or right_core in left_core:
            return True
        if SequenceMatcher(None, left_core, right_core).ratio() >= 0.65:
            return True
    left_terms = _comparison_terms(left)
    right_terms = _comparison_terms(right)
    if not left_terms or not right_terms:
        return False
    overlap = len(left_terms & right_terms)
    return overlap >= 2 and overlap / max(1, min(len(left_terms), len(right_terms))) >= 0.5


def _comparison_terms(text: str) -> set[str]:
    terms: set[str] = set()
    for token in re.findall(r"[a-z0-9_.:/-]{2,}", text):
        if token not in {"the", "and", "with", "that", "this", "not", "does"}:
            terms.add(token)
    for token in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        for marker in _NEGATION_MARKERS:
            token = token.replace(marker.strip(), "")
        if len(token) >= 2:
            terms.add(token)
    return terms


def _conflict_core(text: str) -> str:
    value = f" {normalize_memory_text(text)} "
    for marker in (*_NEGATION_MARKERS, *_ASSERTION_MARKERS):
        value = value.replace(marker, " ")
    value = re.sub(r"(?i)\b(?:does|do|not|never|the|a|an)\b", " ", value)
    value = re.sub(r"[\s，。,.!?！？;；:：]+", "", value)
    return value


def memory_topic_slug(value: str, *, fallback: str = "memory") -> str:
    """Return a bounded filesystem-safe slug without accepting separators."""
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", normalized).strip("-")
    if not normalized:
        normalized = fallback
    normalized = normalized[:64].strip("-") or fallback
    return normalized


def _entry_topic_slug(entry: MemoryEntry) -> str:
    explicit = str(entry.topic or "").strip()
    if explicit:
        return memory_topic_slug(explicit)
    # Implicit topics remain entry-specific, preventing unrelated notes from
    # being silently merged merely because their first words share a slug.
    return f"{memory_topic_slug(entry.text, fallback='memory')}-{entry.id[-8:]}"


def _one_line(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:max(0, limit - 1)].rstrip() + "…"

