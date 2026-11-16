"""把外部入口适配为现有 Tool 协议；不引入第二套插件协议。"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Awaitable, Iterable

from ..tools.base import Tool, ToolResult


class PluginLoadError(RuntimeError):
    """单个插件入口加载失败。"""


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_]+")


def namespace_tool_name(plugin_name: str, tool_name: str) -> str:
    plugin = _SAFE_NAME_RE.sub("_", plugin_name).strip("_") or "plugin"
    tool = _SAFE_NAME_RE.sub("_", tool_name).strip("_") or "tool"
    return f"plugin__{plugin}__{tool}"


class NamespacedTool:
    """工具命名空间代理；调用仍落到插件原始 Tool.execute。"""

    def __init__(self, plugin_name: str, version: str, tool: Tool) -> None:
        self.plugin_name = plugin_name
        self.plugin_version = version
        self.source_name = tool.name
        self._tool = tool
        self.name = namespace_tool_name(plugin_name, tool.name)
        description = str(getattr(tool, "description", "") or "")
        prefix = f"[plugin:{plugin_name}@{version}] "
        self.description = prefix + description
        self.parameters = getattr(tool, "parameters", {})
        self.read_only = bool(getattr(tool, "read_only", False))
        self.required_feature = getattr(tool, "required_feature", None)
        self.run_modes = getattr(tool, "run_modes", None)

    async def execute(self, args: dict[str, Any], ctx) -> ToolResult:
        result = self._tool.execute(args, ctx)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, ToolResult):
            raise TypeError(
                f"插件工具 {self.plugin_name}/{self.source_name} 必须返回 ToolResult")
        return result


def _sha(path: Path) -> str:
    return hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()[:10]


def _load_module(root: Path, module_name: str, plugin_name: str) -> ModuleType:
    """按模块路径加载插件，支持相对导入与同名插件隔离。"""
    parts = module_name.split(".")
    for part in parts:
        if not part.isidentifier():
            raise PluginLoadError(f"非法模块名: {module_name}")
    prefix = f"_krelcode_plugin_{_SAFE_NAME_RE.sub('_', plugin_name)}_{_sha(root)}"
    current_name = prefix
    current_dir = root
    module: ModuleType | None = None
    for index, part in enumerate(parts):
        directory = current_dir / part
        package_init = directory / "__init__.py"
        module_file = current_dir / f"{part}.py"
        is_package = package_init.is_file()
        is_last = index == len(parts) - 1
        if is_package:
            target = package_init
        elif is_last and module_file.is_file():
            target = module_file
        else:
            raise PluginLoadError(
                f"找不到模块 {module_name!r} 的 {package_init} 或 {module_file}")
        synthetic_name = f"{current_name}.{part}" if current_name else part
        spec = importlib.util.spec_from_file_location(
            synthetic_name,
            target,
            submodule_search_locations=(
                [str(directory)] if is_package else None),
        )
        if spec is None or spec.loader is None:
            raise PluginLoadError(f"无法为模块创建 loader: {target}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[synthetic_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(synthetic_name, None)
            raise
        current_name = synthetic_name
        if is_package:
            current_dir = directory
    if module is None:
        raise PluginLoadError(f"无法加载模块: {module_name}")
    return module


def _coerce_tools(value: Any) -> list[Tool]:
    if value is None:
        return []
    if _is_tool(value):
        return [value]
    if isinstance(value, (list, tuple)):
        tools = list(value)
    else:
        try:
            tools = list(value)
        except TypeError as exc:
            raise PluginLoadError("entrypoint 必须返回 Tool、Tool 列表或工厂") from exc
    for tool in tools:
        if not _is_tool(tool):
            raise PluginLoadError(
                f"插件返回了无效 Tool 对象: {type(tool).__name__}")
    return tools


def _is_tool(value: Any) -> bool:
    return (
        isinstance(getattr(value, "name", None), str)
        and bool(getattr(value, "name", "").strip())
        and isinstance(getattr(value, "description", None), str)
        and isinstance(getattr(value, "parameters", None), dict)
        and callable(getattr(value, "execute", None))
    )


async def load_entrypoint(
    root: Path,
    entrypoint: str,
    plugin_name: str,
    version: str,
) -> list[NamespacedTool]:
    module_name, _, attribute = entrypoint.partition(":")
    module = _load_module(Path(root), module_name, plugin_name)
    try:
        value = getattr(module, attribute)
    except AttributeError as exc:
        raise PluginLoadError(f"entrypoint 属性不存在: {attribute}") from exc
    if callable(value) and not _is_tool(value):
        value = value()
        if inspect.isawaitable(value):
            value = await value
    tools = _coerce_tools(value)
    seen: set[str] = set()
    namespaced: list[NamespacedTool] = []
    for tool in tools:
        if tool.name in seen:
            raise PluginLoadError(f"插件内工具重名: {tool.name}")
        seen.add(tool.name)
        namespaced.append(NamespacedTool(plugin_name, version, tool))
    return namespaced
