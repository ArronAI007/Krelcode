"""Small dependency-free Markdown renderer for terminal output."""

from __future__ import annotations

import re
from dataclasses import dataclass

_BOLD = "\x1b[1m"
_ITALIC = "\x1b[3m"
_DIM = "\x1b[2m"
_CYAN = "\x1b[36m"
_GREEN = "\x1b[32m"
_RESET = "\x1b[0m"

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_UNORDERED_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_ORDERED_RE = re.compile(r"^(\s*)(\d+\.)\s+(.*)$")
_QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")
_TABLE_RULE_RE = re.compile(
    r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_STRIKE_RE = re.compile(r"~~([^~\n]+)~~")


@dataclass
class MarkdownState:
    in_fence: bool = False
    fence_label: str = ""


def render_markdown(text: str, enabled: bool = True) -> str:
    """Render Markdown to terminal-oriented text while preserving structure."""
    state = MarkdownState()
    rendered = [
        render_markdown_line(line, state, enabled)
        for line in text.split("\n")
    ]
    return "\n".join(rendered)


def render_markdown_line(
    line: str,
    state: MarkdownState,
    enabled: bool = True,
) -> str:
    stripped = line.lstrip()
    if stripped.startswith(("```", "~~~")):
        if state.in_fence:
            state.in_fence = False
            state.fence_label = ""
            return _style("  └─", enabled, _DIM)
        state.in_fence = True
        state.fence_label = stripped[3:].strip()
        label = state.fence_label or "code"
        return _style(f"  ┌─ {label}", enabled, _DIM)
    if state.in_fence:
        return _style(f"  │ {line}", enabled, _GREEN)
    if not line:
        return ""
    if re.fullmatch(r"\s*(?:---+|\*\*\*+|___+)\s*", line):
        return _style("─" * 32, enabled, _DIM)
    heading = _HEADING_RE.match(line)
    if heading:
        title = _inline(heading.group(1), enabled)
        return _style(f"▌ {title}", enabled, _BOLD + _CYAN)
    quote = _QUOTE_RE.match(line)
    if quote:
        return _style("│ ", enabled, _CYAN) + _inline(quote.group(1), enabled)
    task = re.match(r"^(\s*)[-*+]\s+\[([ xX])\]\s+(.*)$", line)
    if task:
        mark = "☑" if task.group(2).lower() == "x" else "☐"
        return f"{task.group(1)}{mark} {_inline(task.group(3), enabled)}"
    unordered = _UNORDERED_RE.match(line)
    if unordered:
        return (
            unordered.group(1)
            + _style("• ", enabled, _CYAN)
            + _inline(unordered.group(2), enabled)
        )
    ordered = _ORDERED_RE.match(line)
    if ordered:
        return (
            ordered.group(1)
            + _style(ordered.group(2) + " ", enabled, _CYAN)
            + _inline(ordered.group(3), enabled)
        )
    if _TABLE_RULE_RE.match(line):
        cells = _table_cells(line)
        return "─".join("─" * max(3, len(cell) + 2) for cell in cells)
    if "|" in line and line.strip().startswith("|"):
        cells = _table_cells(line)
        return " │ ".join(_inline(cell, enabled) for cell in cells)
    return _inline(line, enabled)


def _table_cells(line: str) -> list[str]:
    value = line.strip().strip("|")
    return [cell.strip() for cell in value.split("|")]


def _inline(text: str, enabled: bool) -> str:
    placeholders: list[str] = []

    def protect_code(match: re.Match[str]) -> str:
        placeholders.append(_style(match.group(1), enabled, _CYAN))
        return f"\x00{len(placeholders) - 1}\x00"

    protected = _INLINE_CODE_RE.sub(protect_code, text)
    protected = _LINK_RE.sub(
        lambda match: (
            match.group(1)
            + _style(f" ({match.group(2)})", enabled, _DIM)
        ),
        protected,
    )
    protected = _BOLD_RE.sub(
        lambda match: _style(match.group(1), enabled, _BOLD),
        protected,
    )
    protected = _ITALIC_RE.sub(
        lambda match: _style(match.group(1), enabled, _ITALIC),
        protected,
    )
    protected = _STRIKE_RE.sub(
        lambda match: _style(match.group(1), enabled, _DIM),
        protected,
    )
    for index, value in enumerate(placeholders):
        protected = protected.replace(f"\x00{index}\x00", value)
    return protected


def render_inline(text: str, enabled: bool = True) -> str:
    """Render inline Markdown without interpreting block structure."""
    return _inline(text, enabled)


def _style(text: str, enabled: bool, style: str) -> str:
    if not enabled or not text:
        return text
    return f"{style}{text}{_RESET}"


class MarkdownStream:
    """Flush complete Markdown lines while retaining inline streaming for prose."""

    def __init__(self, enabled: bool = True, *, soft_limit: int = 360) -> None:
        self.enabled = enabled
        self.soft_limit = max(80, soft_limit)
        self.state = MarkdownState()
        self._pending = ""
        self._passthrough_line = False

    def feed(self, text: str) -> str:
        if not text:
            return ""
        if self._passthrough_line:
            passthrough: list[str] = []
            while "\n" in text:
                part, text = text.split("\n", 1)
                if part:
                    passthrough.append(render_inline(part, self.enabled))
                passthrough.append("\n")
                self._passthrough_line = False
                if text and self._looks_markdown_block(text):
                    self._pending = text
                    return "".join(passthrough)
                if text:
                    self._passthrough_line = True
            if text:
                passthrough.append(render_inline(text, self.enabled))
            return "".join(passthrough)
        self._pending += text
        output: list[str] = []
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            output.append(render_markdown_line(
                line, self.state, self.enabled))
            output.append("\n")
        if (
            self._pending
            and not self.state.in_fence
            and not self._looks_markdown_block(self._pending)
        ):
            output.append(render_inline(self._pending, self.enabled))
            self._pending = ""
            self._passthrough_line = True
        elif len(self._pending) >= self.soft_limit and not self.state.in_fence:
            output.append(render_markdown_line(
                self._pending, self.state, self.enabled))
            self._pending = ""
            self._passthrough_line = True
        return "".join(output)

    def finish(self) -> str:
        if not self._pending:
            return ""
        pending = self._pending
        was_passthrough = self._passthrough_line
        self._pending = ""
        self._passthrough_line = False
        return (
            render_inline(pending, self.enabled)
            if was_passthrough
            else render_markdown_line(pending, self.state, self.enabled)
        )

    def reset(self) -> None:
        self.state = MarkdownState()
        self._pending = ""
        self._passthrough_line = False

    @staticmethod
    def _looks_markdown_block(text: str) -> bool:
        stripped = text.lstrip()
        return bool(
            stripped.startswith(("#", ">", "|", "```", "~~~"))
            or re.match(r"^(?:[-*+]\s+|\d+\.\s+)", stripped)
            or "**" in text
            or "`" in text
            or "~~" in text
            or re.search(r"\[[^\]\n]+\]\(", text)
        )


__all__ = [
    "MarkdownState",
    "MarkdownStream",
    "render_markdown",
    "render_markdown_line",
    "render_inline",
]
