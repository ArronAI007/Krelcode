"""零依赖 SKILL.md frontmatter 解析。

支持 Agent Skills 常用的 YAML 子集：顶层 scalar、布尔、引号字符串、
inline list、缩进列表，以及 `|` / `>` block scalar。只读取 frontmatter，
正文保持原样，避免把完整 skill 内容提前注入模型上下文。
"""

from __future__ import annotations

import ast
import csv
import re
from typing import Any, Dict, List, Tuple


class SkillParseError(ValueError):
    pass


def parse_skill_document(text: str, fallback_name: str = "") -> Tuple[Dict[str, Any], str]:
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise SkillParseError("SKILL.md 必须以 YAML frontmatter 开头（---）")
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        raise SkillParseError("SKILL.md frontmatter 缺少结束分隔线（---）")

    metadata = _parse_frontmatter(lines[1:end])
    body = "".join(lines[end + 1:]).lstrip("\r\n")
    if not metadata.get("name") and fallback_name:
        metadata["name"] = fallback_name
    return metadata, body


def _parse_frontmatter(lines: List[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    i = 0
    while i < len(lines):
        raw = lines[i].rstrip("\r\n")
        if not raw.strip() or raw.lstrip().startswith("#"):
            i += 1
            continue
        if raw[:1].isspace() or ":" not in raw:
            i += 1
            continue
        key, value = raw.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            i += 1
            continue

        if value in ("|", ">", "|-", ">-", "|+", ">+"):
            style = value[0]
            consumed, block = _read_block_scalar(lines, i + 1, style)
            out[key] = block
            i = consumed
            continue

        if not value:
            consumed, items = _read_list(lines, i + 1)
            if items:
                out[key] = items
                i = consumed
                continue

        out[key] = _parse_scalar(value)
        i += 1
    return out


def _read_block_scalar(lines: List[str], start: int, style: str) -> Tuple[int, str]:
    gathered: List[str] = []
    i = start
    while i < len(lines):
        raw = lines[i].rstrip("\r\n")
        if raw.strip() and not raw[:1].isspace():
            break
        gathered.append(raw)
        i += 1
    while gathered and not gathered[-1].strip():
        gathered.pop()
    nonempty = [line for line in gathered if line.strip()]
    indent = min((len(line) - len(line.lstrip(" ")) for line in nonempty), default=0)
    stripped = [line[indent:] if len(line) >= indent else "" for line in gathered]
    if style == ">":
        paragraphs: List[str] = []
        current: List[str] = []
        for line in stripped:
            if line.strip():
                current.append(line.strip())
            else:
                if current:
                    paragraphs.append(" ".join(current))
                    current = []
                paragraphs.append("")
        if current:
            paragraphs.append(" ".join(current))
        return i, "\n".join(paragraphs).strip()
    return i, "\n".join(stripped).rstrip()


def _read_list(lines: List[str], start: int) -> Tuple[int, List[str]]:
    items: List[str] = []
    i = start
    while i < len(lines):
        raw = lines[i].rstrip("\r\n")
        stripped = raw.strip()
        if not stripped:
            i += 1
            continue
        if not raw[:1].isspace():
            break
        if stripped.startswith("- "):
            items.append(str(_parse_scalar(stripped[2:].strip())))
            i += 1
            continue
        break
    return i, items


def _parse_scalar(raw: str) -> Any:
    value = raw.strip()
    if not value:
        return ""
    lowered = value.lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"null", "~"}:
        return None
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        try:
            return [_parse_scalar(x) for x in next(csv.reader([inner], skipinitialspace=True))]
        except (csv.Error, StopIteration):
            return [x.strip() for x in inner.split(",")]
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        try:
            return ast.literal_eval(value)
        except (ValueError, SyntaxError):
            return value[1:-1]
    return value


def split_allowed_tools(value: Any) -> Tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, list):
        return tuple(str(v).strip() for v in value if str(v).strip())
    text = str(value).strip()
    if not text:
        return ()
    if "," in text:
        return tuple(part.strip() for part in text.split(",") if part.strip())
    return (text,)
