"""Complete Krelcode CLI: parser, command routing and REPL startup.

The lightweight ``krelcode.entrypoints.cli`` fast path imports this module only
when it is needed. Command-specific dependencies stay inside route functions;
the Agent composition root (``krelcode.app``) is imported exclusively for the
conversation runner.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from pathlib import Path

from krelcode import __version__
from krelcode.agent.compression import DEFAULT_MAX_TOKENS as DEFAULT_CONTEXT_BUDGET
from krelcode.agent.loop import DEFAULT_MAX_STEPS


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="krelcode", description="Krelcode coding agent (统一 Provider + tool-calling 循环 + MCP)"
    )
    p.add_argument("command", nargs="?", choices=("doctor", "init", "review"), default=None,
                   help="子命令：doctor=环境自检；init=生成配置/规则模板；"
                        "review=评审当前 git 变更（缺省进入对话）")
    p.add_argument("--force", action="store_true",
                   help="init 子命令：覆盖已存在的模板文件")
    p.add_argument("--staged", action="store_true",
                   help="review 子命令：只评审已暂存变更（git diff --cached）")
    p.add_argument("--focus", default=None,
                   help="review 子命令：附加评审重点（如 '安全' / '性能'）")
    p.add_argument("--provider", default=None,
                   help="openai / anthropic / gemini / mock（默认: KRELCODE_PROVIDER 或配置文件 default_provider 或 openai）")
    p.add_argument("--mock", action="store_true", help="等价于 --provider mock（含演示脚本），无需 API key")
    p.add_argument("--model", default=None, help="模型名（默认: 配置文件 / KRELCODE_MODEL / provider 默认）")
    p.add_argument("--base-url", default=None, help="覆盖 provider 的 base_url（任意兼容端点）")
    p.add_argument("--config", default=None, help="TOML 配置文件路径（默认 krelcode.toml 或 ~/.krelcode/config.toml）")
    p.add_argument("--workspace", default=".", help="工作区根目录（默认当前目录）")
    p.add_argument(
        "--max-steps", type=int, default=DEFAULT_MAX_STEPS,
        help=f"单轮任务的最大 LLM 迭代步数（默认 {DEFAULT_MAX_STEPS}）",
    )
    p.add_argument("--repeat-failure-limit", type=int, default=3,
                   help="同一调用连续失败多少次判定卡死并中止（默认 3）")
    p.add_argument("--token-budget", type=int, default=None,
                   help="单轮任务的 token 预算，超限中止（默认不限）")
    p.add_argument("--retries", type=int, default=2,
                   help="LLM 瞬态失败（429/5xx/网络）自动重试次数，指数退避（默认 2，0 关闭）")
    p.add_argument("--no-context", action="store_true",
                   help="跳过启动时的工作区上下文注入（目录树/git/AGENTS.md）")
    p.add_argument("--no-skills", action="store_true",
                   help="禁用 Agent Skills 发现、索引与相关工具")
    p.add_argument("--skills-dir", action="append", default=[],
                   help="额外 skill 根目录（可重复；优先级高于项目/全局目录）")
    p.add_argument("--auto-approve", action="store_true",
                   help="ASK 级操作自动批准（信任模式；DENY 级仍然拒绝）")
    p.add_argument("--sandbox-backend", choices=("auto", "static", "seatbelt", "bubblewrap"),
                   default=None, help="运行时 Sandbox 后端（默认 auto；显式后端不可用则启动失败）")
    p.add_argument("--sandbox-network", action="store_true", default=None,
                   help="允许 Sandbox 子进程访问网络（默认关闭）")
    p.add_argument("--no-sandbox", action="store_true",
                   help="关闭审批策略与运行时 Sandbox（AllowAll，仅建议在隔离环境中使用）")
    p.add_argument("--context-tokens", type=int, default=None,
                   help="上下文 token 预算，超限自动压缩（默认: 模型上下文窗口×75%%，"
                        f"未知模型回退 {DEFAULT_CONTEXT_BUDGET}）")
    p.add_argument("--no-compress", action="store_true",
                   help="关闭上下文自动压缩")
    p.add_argument("--no-mcp", action="store_true",
                   help="不启动配置文件中的 MCP 服务器")
    p.add_argument("--resume", nargs="?", const="latest", default=None,
                   help="恢复会话：--resume 取最新，--resume <path> 取指定文件")
    p.add_argument("--no-session", action="store_true",
                   help="关闭会话 JSONL 录制（默认写入 <workspace>/.krelcode/sessions/）")
    p.add_argument("--no-checkpoint", action="store_true",
                   help="关闭文件检查点（/undo 回滚功能）")
    p.add_argument("--auto-commit", action="store_true",
                   help="每轮结束若有文件变更自动 git 提交（需工作区是 git 仓库）")
    p.add_argument("-m", "--message", default=None, help="单次提问模式：执行完直接退出")
    p.add_argument("--output-format", choices=("text", "json"), default="text",
                   help="配合 -m：json 输出结构化结果到 stdout（启动提示走 stderr），适合 CI/脚本")
    p.add_argument("--version", action="version", version=f"krelcode {__version__}")
    return p


def _run_doctor(args: argparse.Namespace) -> int:
    from krelcode.config import load_config
    from krelcode.doctor import exit_code, format_report, run_all_checks

    config_path = Path(args.config) if args.config else None
    config = load_config(config_path if config_path and config_path.is_file() else None)
    results = run_all_checks(Path(args.workspace).resolve(), config, config_path)
    print(format_report(results))
    return exit_code(results)


def _run_init(args: argparse.Namespace) -> int:
    from krelcode.scaffold import scaffold_workspace

    try:
        for action in scaffold_workspace(Path(args.workspace).resolve(), force=args.force):
            print(action)
    except OSError as e:
        print(f"init 失败: {e}")
        return 1
    print("完成。下一步：设置 API key 后运行 `krelcode doctor` 验证环境。")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(None if argv is None else list(argv))
    if args.command == "doctor":
        return _run_doctor(args)
    if args.command == "init":
        return _run_init(args)

    try:
        if args.command == "review":
            from krelcode.review import run_review

            return asyncio.run(run_review(args))

        # Agent/Provider/MCP/Sandbox composition stays behind the full CLI
        # boundary and is loaded only for conversation/headless runs.
        from krelcode.app import async_main

        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        # asyncio.run() has already cancelled the task and cleaned resources.
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
