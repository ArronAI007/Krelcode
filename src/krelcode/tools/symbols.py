"""Incremental repo-map symbol extraction.

Python always uses the standard-library AST because it provides exact
signatures and preserves the existing zero-dependency behavior. JavaScript and
TypeScript use tree-sitter when the optional syntax extra is installed; when it
is absent or cannot load a grammar, a conservative regex extractor remains the
fallback.
"""

from __future__ import annotations

import ast
import importlib
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, List, Optional, Protocol, Sequence, Tuple, cast

Symbol = Tuple[str, int, str]

PYTHON_EXTENSIONS = frozenset({".py", ".pyi"})
JAVASCRIPT_EXTENSIONS = frozenset({".js", ".jsx", ".mjs", ".cjs"})
TYPESCRIPT_EXTENSIONS = frozenset({".ts", ".tsx", ".mts", ".cts"})
SOURCE_EXTENSIONS = PYTHON_EXTENSIONS | JAVASCRIPT_EXTENSIONS | TYPESCRIPT_EXTENSIONS
SIGNATURE_MAX_CHARS = 100


class _TreeSitterPoint(Protocol):
    row: int


class _TreeSitterNode(Protocol):
    type: str
    named_children: Sequence["_TreeSitterNode"]
    start_byte: int
    end_byte: int
    start_point: _TreeSitterPoint

    def child_by_field_name(self, field: str) -> Optional["_TreeSitterNode"]: ...


class _TreeSitterTree(Protocol):
    root_node: _TreeSitterNode


JS_PATTERNS = (
    (re.compile(r"^\s*(?:export\s+)?(?:declare\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)"), "class"),
    (re.compile(r"^\s*(?:export\s+)?(?:declare\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)"), "function"),
    (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"), "const"),
    (re.compile(r"^\s*(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?function\b"), "function"),
    (re.compile(r"^\s*(?:export\s+)?(?:declare\s+)?interface\s+([A-Za-z_$][\w$]*)"), "interface"),
    (re.compile(r"^\s*(?:export\s+)?(?:declare\s+)?type\s+([A-Za-z_$][\w$]*)\s*="), "type"),
    (re.compile(r"^\s*(?:export\s+)?(?:declare\s+)?(?:const\s+)?enum\s+([A-Za-z_$][\w$]*)"), "enum"),
    (re.compile(r"^\s*(?:export\s+)?(?:declare\s+)?namespace\s+([A-Za-z_$][\w$]*)"), "namespace"),
)


@dataclass(frozen=True)
class ParsedSymbols:
    symbols: Tuple[Symbol, ...]
    backend: str


def parse_symbols(path: Path, source: str) -> ParsedSymbols:
    """Parse one source file without touching disk or mutating shared state."""
    suffix = path.suffix.lower()
    if suffix in PYTHON_EXTENSIONS:
        return ParsedSymbols(tuple(_python_symbols(source)), "python-ast")
    if suffix in JAVASCRIPT_EXTENSIONS | TYPESCRIPT_EXTENSIONS:
        tree_symbols = _tree_sitter_symbols(source, suffix)
        if tree_symbols is not None:
            family = "typescript" if suffix in TYPESCRIPT_EXTENSIONS else "javascript"
            return ParsedSymbols(tree_symbols, f"tree-sitter-{family}")
        return ParsedSymbols(tuple(_js_symbols(source)), "regex")
    return ParsedSymbols((), "unsupported")


def tree_sitter_available() -> bool:
    """Return whether the optional base parser package can be imported."""
    try:
        importlib.import_module("tree_sitter")
    except (ImportError, OSError):
        return False
    return True


def _python_symbols(source: str) -> List[Symbol]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    out: List[Symbol] = []

    def signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
        names: List[str] = []
        a = node.args
        all_args = list(a.posonlyargs) + list(a.args)
        for arg in all_args:
            names.append(arg.arg)
        if a.vararg:
            names.append("*" + a.vararg.arg)
        elif a.kwonlyargs:
            names.append("*")
        for arg in a.kwonlyargs:
            names.append(arg.arg)
        if a.kwarg:
            names.append("**" + a.kwarg.arg)
        sig = f"{node.name}({', '.join(names)})"
        if len(sig) > SIGNATURE_MAX_CHARS:
            sig = sig[:SIGNATURE_MAX_CHARS] + "…)"
        return sig

    def walk(body: Sequence[ast.stmt], indent: str) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                out.append((indent, node.lineno, f"class {node.name}"))
                walk(node.body, indent + "  ")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                prefix = (
                    "async def "
                    if isinstance(node, ast.AsyncFunctionDef) else "def "
                )
                out.append((indent, node.lineno, prefix + signature(node)))

    walk(tree.body, "")
    return out


@lru_cache(maxsize=8)
def _load_language(suffix: str) -> Optional[object]:
    module_name, factory_name = _language_spec(suffix)
    module = importlib.import_module(module_name)
    factory = getattr(module, factory_name)
    raw = factory()
    try:
        from tree_sitter import Language  # type: ignore[import-untyped]
    except (ImportError, OSError):
        return None
    try:
        return cast(object, Language(raw))
    except TypeError:
        return cast(object, raw)


def _language_spec(suffix: str) -> Tuple[str, str]:
    if suffix == ".tsx":
        return "tree_sitter_typescript", "language_tsx"
    if suffix in TYPESCRIPT_EXTENSIONS:
        return "tree_sitter_typescript", "language_typescript"
    return "tree_sitter_javascript", "language"


def _tree_sitter_symbols(source: str, suffix: str) -> Optional[Tuple[Symbol, ...]]:
    try:
        language = _load_language(suffix)
        if language is None:
            return None
        from tree_sitter import Parser
        parser: Any
        try:
            parser = Parser(language)
        except TypeError:
            parser = Parser()
            parser.set_language(language)
        tree = cast(_TreeSitterTree, parser.parse(source.encode("utf-8")))
    except (ImportError, OSError, AttributeError, TypeError, ValueError):
        return None
    out: List[Symbol] = []
    _walk_tree_sitter(tree.root_node, source.encode("utf-8"), out, depth=0)
    return tuple(out)


def _walk_tree_sitter(
    node: _TreeSitterNode,
    source: bytes,
    out: List[Symbol],
    *,
    depth: int,
) -> None:
    kind = node.type
    if kind in {"program", "export_statement", "ambient_declaration"}:
        for child in node.named_children:
            _walk_tree_sitter(child, source, out, depth=depth)
        return

    if kind in {
        "class_declaration", "abstract_class_declaration", "class",
        "interface_declaration", "enum_declaration", "type_alias_declaration",
        "internal_module", "module",
    }:
        label = {
            "class_declaration": "class",
            "abstract_class_declaration": "class",
            "class": "class",
            "interface_declaration": "interface",
            "enum_declaration": "enum",
            "type_alias_declaration": "type",
            "internal_module": "namespace",
            "module": "namespace",
        }[kind]
        name = _field_text(node, "name", source)
        if name:
            out.append((_indent(depth), _line(node), _bounded(f"{label} {name}")))
        body = (
            node.child_by_field_name("body")
            or _first_named_child(node, {"class_body", "interface_body", "enum_body", "declaration_list"})
        )
        if body is not None:
            for child in body.named_children:
                _walk_tree_sitter(child, source, out, depth=depth + 1)
        return

    if kind in {
        "function_declaration", "generator_function_declaration",
        "function_signature", "method_definition", "method_signature",
        "abstract_method_signature",
    }:
        name = _field_text(node, "name", source)
        if name:
            label = "method" if "method" in kind else "function"
            out.append((_indent(depth), _line(node), _bounded(f"{label} {name}")))
        return

    if kind in {"lexical_declaration", "variable_declaration"}:
        for child in node.named_children:
            _walk_tree_sitter(child, source, out, depth=depth)
        return

    if kind in {
        "public_field_definition", "field_definition", "property_definition",
    }:
        value = node.child_by_field_name("value")
        if value is not None and value.type in {
            "arrow_function", "function_expression", "function", "generator_function",
        }:
            name = _field_text(node, "name", source)
            if name:
                out.append((_indent(depth), _line(node), _bounded(f"field {name}")))
        return

    if kind == "variable_declarator":
        value = node.child_by_field_name("value")
        if value is not None and value.type in {
            "arrow_function", "function_expression", "function", "generator_function",
        }:
            name = _field_text(node, "name", source)
            if name:
                label = "const" if value.type != "function" else "function"
                out.append((_indent(depth), _line(node), _bounded(f"{label} {name}")))
        return


def _field_text(node: _TreeSitterNode, field: str, source: bytes) -> str:
    child = node.child_by_field_name(field)
    if child is None:
        return ""
    return source[child.start_byte:child.end_byte].decode("utf-8", errors="replace").strip()


def _first_named_child(
    node: _TreeSitterNode,
    kinds: set[str],
) -> Optional[_TreeSitterNode]:
    for child in node.named_children:
        if child.type in kinds:
            return child
    return None


def _line(node: _TreeSitterNode) -> int:
    return int(node.start_point.row) + 1


def _indent(depth: int) -> str:
    return "  " * max(0, depth)


def _bounded(text: str) -> str:
    if len(text) > SIGNATURE_MAX_CHARS:
        return text[:SIGNATURE_MAX_CHARS] + "…"
    return text


def _js_symbols(source: str) -> List[Symbol]:
    out: List[Symbol] = []
    for lineno, line in enumerate(source.splitlines(), start=1):
        for pattern, kind in JS_PATTERNS:
            match = pattern.match(line)
            if match:
                out.append(("", lineno, _bounded(f"{kind} {match.group(1)}")))
                break
    return out


__all__ = [
    "JAVASCRIPT_EXTENSIONS",
    "PYTHON_EXTENSIONS",
    "SOURCE_EXTENSIONS",
    "TYPESCRIPT_EXTENSIONS",
    "ParsedSymbols",
    "Symbol",
    "extract_symbols",
    "parse_symbols",
    "tree_sitter_available",
]


def extract_symbols(path: Path) -> List[Symbol]:
    """Compatibility helper for callers that only need the symbol tuples."""
    try:
        source = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    return list(parse_symbols(path, source).symbols)
