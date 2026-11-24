"""Built-in tool catalog composition."""

from __future__ import annotations

from typing import List, Optional

from krelcode.browser import BrowserRuntime
from krelcode.contracts.tool import Tool
from krelcode.lsp.manager import LSPManager
from krelcode.lsp.tools import build_lsp_tools
from krelcode.session.memory import MemoryStore
from krelcode.session.memory_tool import MemorySearchTool
from krelcode.tools.builtin.check_syntax import CheckSyntaxTool
from krelcode.tools.builtin.database_migration import (
    MigrationOptions,
    build_migration_tools,
)
from krelcode.tools.builtin.edit_file import EditFileTool
from krelcode.tools.builtin.find_refs import FindRefsTool
from krelcode.tools.builtin.glob import GlobTool
from krelcode.tools.builtin.grep import GrepTool
from krelcode.tools.builtin.job_tools import (
    JobKillTool,
    JobListTool,
    JobOutputTool,
    JobStartTool,
)
from krelcode.tools.builtin.notebook import NotebookOptions, build_notebook_tools
from krelcode.tools.builtin.read_file import ReadFileTool
from krelcode.tools.builtin.repo_map import RepoMapTool
from krelcode.tools.builtin.run_command import RunCommandTool
from krelcode.tools.builtin.time_tool import GetTimeTool
from krelcode.tools.builtin.tool_search import ToolSearchTool
from krelcode.tools.builtin.write_file import WriteFileTool
from krelcode.tools.extension.ask_user import AskUserTool
from krelcode.tools.extension.update_plan import UpdatePlanTool
from krelcode.web.tools import WebRuntime


def default_tools(
    memory_store: Optional[MemoryStore] = None,
    *,
    lsp_manager: Optional[LSPManager] = None,
    web_runtime: Optional[WebRuntime] = None,
    browser_runtime: Optional[BrowserRuntime] = None,
    notebook_options: Optional[NotebookOptions] = None,
    migration_options: Optional[MigrationOptions] = None,
) -> List[Tool]:
    """内置工具集（17 个 + 9 个子代理工具在 build_loop 注册）。
    MCP 远程工具在运行时追加注册；Memory、Web、Browser、Notebook 与数据库
    迁移能力只在对应运行时/配置显式传入时装配，保持基础工具集向后兼容。"""
    tools: List[Tool] = [
        GetTimeTool(),
        ReadFileTool(),
        WriteFileTool(),
        EditFileTool(),
        RunCommandTool(),
        GrepTool(),
        GlobTool(),
        RepoMapTool(),
        FindRefsTool(),
        CheckSyntaxTool(),
        AskUserTool(),
        UpdatePlanTool(),
        JobStartTool(),
        JobOutputTool(),
        JobListTool(),
        JobKillTool(),
        ToolSearchTool(),
    ]
    if memory_store is not None:
        tools.append(MemorySearchTool(memory_store))
    if lsp_manager is not None and lsp_manager.enabled:
        tools.extend(build_lsp_tools(lsp_manager))
    if web_runtime is not None and web_runtime.options.enabled:
        tools.extend(web_runtime.build_tools())
    if browser_runtime is not None and browser_runtime.options.enabled:
        from ...browser.tools import build_browser_tools

        tools.extend(build_browser_tools(browser_runtime))
    if notebook_options is not None and notebook_options.enabled:
        tools.extend(build_notebook_tools(notebook_options))
    if migration_options is not None and migration_options.enabled:
        tools.extend(build_migration_tools(migration_options))
    return tools
