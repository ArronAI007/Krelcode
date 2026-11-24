"""Built-in tool catalog composition."""

from __future__ import annotations

from ._deps import *  # noqa: F401,F403


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
        GetTimeTool(), ReadFileTool(), WriteFileTool(), EditFileTool(),
        RunCommandTool(), GrepTool(), GlobTool(), RepoMapTool(),
        FindRefsTool(), CheckSyntaxTool(), AskUserTool(), UpdatePlanTool(),
        JobStartTool(), JobOutputTool(), JobListTool(), JobKillTool(),
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

