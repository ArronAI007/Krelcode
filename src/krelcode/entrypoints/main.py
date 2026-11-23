"""Complete Krelcode CLI: parser, command routing and REPL startup.

The lightweight ``krelcode.entrypoints.cli`` fast path imports this module only
when it is needed. Command-specific dependencies stay inside route functions;
the Agent composition root (``krelcode.app``) is imported exclusively for the
conversation runner.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from krelcode import __version__
from krelcode.agent.compression import DEFAULT_MAX_TOKENS as DEFAULT_CONTEXT_BUDGET
from krelcode.agent.loop import DEFAULT_MAX_STEPS
from krelcode.core.errors import ConfigError


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="krelcode", description="Krelcode coding agent (统一 Provider + tool-calling 循环 + MCP)"
    )
    p.add_argument("command", nargs="?",
                   choices=("doctor", "init", "review", "security-review", "benchmark",
                            "daemon", "jobs", "agents"),
                   default=None,
                   help="子命令：doctor=环境自检；init=生成配置/规则模板；"
                        "review=评审当前 git 变更；security-review=只读安全审查；"
                        "benchmark=真实模型 Prompt 行为评测；daemon/jobs/agents="
                        "持久化后台任务与子代理；缺省进入对话")
    p.add_argument("action", nargs="?", default=None,
                   help="daemon/jobs/agents 子操作（start/status/stop/list/output/...）")
    p.add_argument("target", nargs="?", default=None,
                   help="子操作目标（job id、agent id、sidechain 等）")
    p.add_argument("--force", action="store_true",
                   help="init 子命令：覆盖已存在的模板文件")
    p.add_argument("--staged", action="store_true",
                   help="review/security-review 子命令：只处理已暂存变更（git diff --cached）")
    p.add_argument("--focus", default=None,
                   help="review/security-review 子命令：附加评审重点")
    p.add_argument(
        "--fail-on", choices=("never", "info", "low", "medium", "high", "critical"),
        default="high",
        help="security-review 子命令：达到该严重度时返回非零退出码（默认 high）")
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
    p.add_argument("--output-style", default=None,
                   help="当前 Output Style（内置 default/explanatory/learning，或自定义名称；"
                        "覆盖 [output_styles].active）")
    p.add_argument("--output-style-dir", action="append", default=[],
                   help="额外 Output Style 文件或目录（可重复）")
    p.add_argument("--no-output-styles", action="store_true",
                   help="禁用 Output Styles 发现与注入")
    p.add_argument("--no-web", action="store_true",
                   help="禁用内置 WebFetch/WebSearch 与浏览器网络工具")
    p.add_argument("--no-browser", action="store_true",
                   help="禁用浏览器会话工具（不影响 WebFetch/WebSearch）")
    p.add_argument("--no-notebook", action="store_true",
                   help="禁用 Jupyter Notebook inspect/edit/create 工具")
    p.add_argument("--no-database", action="store_true",
                   help="禁用数据库迁移 inspect/validate/create 工具")
    p.add_argument("--no-plugins", action="store_true",
                   help="禁用本地目录插件发现与插件工具")
    p.add_argument("--plugins-dir", action="append", default=[],
                   help="额外插件根目录（可重复；优先级高于项目/全局目录）")
    p.add_argument("--auto-approve", action="store_true",
                   help="ASK 级操作自动批准（信任模式；DENY 级仍然拒绝）")
    p.add_argument("--no-diff-review", action="store_true",
                   help="关闭 REPL 文件写入前的交互式 diff 审阅（仍保留安全审批）")
    daemon_group = p.add_mutually_exclusive_group()
    daemon_group.add_argument(
        "--daemon", dest="daemon_enabled", action="store_true", default=None,
        help="启用持久化 daemon：后台 Job/子代理在 REPL 退出后继续运行")
    daemon_group.add_argument(
        "--no-daemon", dest="daemon_enabled", action="store_false",
        help="禁用 daemon，使用兼容的进程内 Job/SubagentManager")
    p.add_argument("--daemon-idle-timeout", type=float, default=None,
                   help="daemon 无活动任务后的退出秒数（0 表示不自动退出）")
    p.add_argument("--foreground", action="store_true",
                   help="daemon start：前台运行（默认后台启动）")
    p.add_argument("--command", dest="job_command", default=None,
                   help="jobs start：要执行的后台 shell 命令")
    p.add_argument("--task", default=None,
                   help="agents spawn：子代理任务描述")
    p.add_argument("--definition", default=None,
                   help="agents spawn：Agent Definition 名称")
    p.add_argument("--after", type=int, default=0,
                   help="jobs output/agents feed：起始 sequence")
    p.add_argument("--wait", type=float, default=0.0,
                   help="jobs output：等待新输出的秒数")
    p.add_argument("--isolated", action="store_true",
                   help="agents spawn：在独立 git worktree 中运行")
    p.add_argument("--fork-context", action="store_true",
                   help="agents spawn：继承指令与筛选 Memory（不继承完整历史）")
    p.add_argument("--permission-mode",
                   choices=("default", "read-only", "auto-approve", "plan"),
                   default=None,
                   help="工具权限模式（默认: 配置文件 permissions.mode 或 default）")
    p.add_argument("--sandbox-backend", choices=("auto", "static", "seatbelt", "bubblewrap"),
                   default=None, help="运行时 Sandbox 后端（默认 auto；显式后端不可用则启动失败）")
    p.add_argument("--sandbox-network", action="store_true", default=None,
                   help="允许 Sandbox 子进程访问网络（默认关闭）")
    p.add_argument("--no-sandbox", action="store_true",
                   help="关闭审批策略与运行时 Sandbox（AllowAll，仅建议在隔离环境中使用）")
    p.add_argument("--memory-mode", choices=("off", "suggest", "auto"), default=None,
                   help="Auto Memory 模式（默认: [memory].mode 或 suggest；"
                        "suggest 仅入队等待 /memory review）")
    p.add_argument("--memory-suggest-threshold", type=float, default=None,
                   help="Auto Memory 候选入队最低置信度（默认 0.55）")
    p.add_argument("--memory-auto-threshold", type=float, default=None,
                   help="auto 模式自动写入最低置信度（默认 0.80）")
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
    p.add_argument(
        "--output-format", choices=("text", "json", "stream-json"),
        default="text",
        help="text 普通文本；json 单条结构化结果；stream-json 逐行 JSON 事件流",
    )
    p.add_argument(
        "--input-format", choices=("text", "stream-json"), default="text",
        help="text 普通 REPL/-m；stream-json 从 stdin 逐行读取用户消息（SDK/管道）",
    )
    p.add_argument("--version", action="version", version=f"krelcode {__version__}")
    return p


def _run_doctor(args: argparse.Namespace) -> int:
    from krelcode.config import load_config_layers
    from krelcode.core.errors import ConfigError
    from krelcode.doctor import exit_code, format_report, run_all_checks

    config_path = Path(args.config).expanduser() if args.config else None
    try:
        bundle = load_config_layers(
            workspace=Path(args.workspace),
            explicit_path=config_path,
        )
    except ConfigError as exc:
        print(f"配置错误: {exc}")
        return 1
    results = run_all_checks(
        Path(args.workspace).resolve(),
        bundle.data,
        config_path,
        config_bundle=bundle,
    )
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


def _daemon_client(args: argparse.Namespace):
    from krelcode.daemon.client import DaemonClient

    config_path = Path(args.config).expanduser() if args.config else None
    return DaemonClient(
        Path(args.workspace).expanduser().resolve(),
        config_path=config_path,
        sandbox_backend=args.sandbox_backend,
        sandbox_network=args.sandbox_network,
        no_sandbox=args.no_sandbox,
        auto_approve=args.auto_approve,
        permission_mode=args.permission_mode,
    )


def _daemon_idle_timeout(args: argparse.Namespace) -> float:
    from krelcode.config import load_config_layers
    from krelcode.daemon.options import resolve_daemon_options

    config_path = Path(args.config).expanduser() if args.config else None
    bundle = load_config_layers(
        workspace=Path(args.workspace), explicit_path=config_path)
    return resolve_daemon_options(args, bundle.data).idle_timeout


def _run_daemon(args: argparse.Namespace) -> int:
    from krelcode.daemon.client import DaemonUnavailable

    action = (args.action or "status").lower()
    client = _daemon_client(args)
    try:
        if action == "start":
            idle = _daemon_idle_timeout(args)
            if args.foreground:
                from krelcode.daemon.server import run_daemon

                config_path = Path(args.config).expanduser() if args.config else None
                asyncio.run(run_daemon(
                    Path(args.workspace).expanduser().resolve(),
                    config_path=config_path,
                    idle_timeout=idle,
                    sandbox_backend=args.sandbox_backend,
                    sandbox_network=args.sandbox_network,
                    no_sandbox=args.no_sandbox,
                    auto_approve=args.auto_approve,
                    permission_mode=args.permission_mode,
                ))
                return 0
            status = asyncio.run(client.start(idle_timeout=idle))
            print(
                f"daemon running instance={status.instance_id} pid={status.pid} "
                f"workspace={status.workspace_root}")
            return 0
        if action == "stop":
            stopped = asyncio.run(client.stop())
            print("daemon stopped" if stopped else "daemon 未运行或停止超时")
            return 0 if stopped else 1
        if action == "restart":
            asyncio.run(client.stop())
            status = asyncio.run(client.start(idle_timeout=_daemon_idle_timeout(args)))
            print(f"daemon restarted instance={status.instance_id} pid={status.pid}")
            return 0
        if action in {"status", "list"}:
            status = asyncio.run(client.status())
            if not status.running:
                print("daemon stopped")
                return 1
            data = status.data
            print(
                f"daemon running instance={status.instance_id} pid={status.pid} "
                f"workspace={status.workspace_root}")
            print(
                f"active_jobs={data.get('active_jobs', 0)} "
                f"durable_jobs={data.get('durable_jobs', 0)} "
                f"agents={data.get('agents', 0)} idle_timeout={data.get('idle_timeout')}")
            if data.get("issues"):
                for issue in data["issues"]:
                    print(f"warning: {issue}")
            return 0
        print("用法: krelcode daemon start|status|stop|restart [--foreground]")
        return 2
    except (DaemonUnavailable, RuntimeError, ValueError, OSError) as exc:
        print(f"daemon 错误: {exc}", file=sys.stderr)
        return 1


def _run_jobs(args: argparse.Namespace) -> int:
    from krelcode.daemon.client import DaemonJobManager, DaemonUnavailable
    from krelcode.jobs import ShellJobError

    action = (args.action or "list").lower()
    manager = DaemonJobManager(
        Path(args.workspace).expanduser().resolve(),
        client=_daemon_client(args),
        idle_timeout=_daemon_idle_timeout(args),
    )
    try:
        if action in {"list", "ls"}:
            jobs = manager.list_jobs()
            if not jobs:
                print("（暂无后台 Job）")
                return 0
            for job in jobs:
                exit_code = "-" if job.exit_code is None else str(job.exit_code)
                command = " ".join(job.command.split())
                if len(command) > 100:
                    command = command[:99] + "…"
                print(
                    f"{job.job_id} {job.status.value:<9} exit={exit_code:<4} "
                    f"pid={job.pid} seq={job.latest_sequence} {command}")
            return 0
        if action == "start":
            command = str(args.job_command or "").strip()
            if not command:
                print("jobs start 需要 --command 或命令文本", file=sys.stderr)
                return 2
            job = asyncio.run(manager.start(
                command, cwd=Path(args.workspace).expanduser().resolve()))
            print(
                f"{job.job_id} {job.status.value} pid={job.pid}\n"
                f"log: {job.log_path}\n"
                f"读取: krelcode jobs output {job.job_id} --wait 5")
            return 0
        if action == "output":
            if not args.target:
                print("用法: krelcode jobs output <job-id> [--after N] [--wait 秒]",
                      file=sys.stderr)
                return 2
            page = asyncio.run(manager.output(
                args.target,
                after_sequence=max(0, int(args.after or 0)),
                wait_seconds=max(0.0, float(args.wait or 0.0)),
            ))
            print(page.render())
            return 0
        if action == "kill":
            if not args.target:
                print("用法: krelcode jobs kill <job-id>", file=sys.stderr)
                return 2
            job = asyncio.run(manager.kill(args.target))
            print(f"{job.job_id} {job.status.value} exit={job.exit_code}")
            return 0
        print("用法: krelcode jobs list|start|output|kill", file=sys.stderr)
        return 2
    except (DaemonUnavailable, ShellJobError, RuntimeError, ValueError, OSError) as exc:
        print(f"jobs 错误: {exc}", file=sys.stderr)
        return 1


def _run_agents(args: argparse.Namespace) -> int:
    from krelcode.daemon.client import DaemonSubagentManager, DaemonUnavailable

    action = (args.action or "list").lower()
    manager = DaemonSubagentManager(
        Path(args.workspace).expanduser().resolve(),
        client=_daemon_client(args),
        idle_timeout=_daemon_idle_timeout(args),
    )
    try:
        if action in {"list", "ls"}:
            lines = manager.status_lines()
            print("\n".join(lines) if lines else "（暂无子代理）")
            return 0
        if action == "definitions":
            lines = manager.definition_lines()
            print("\n".join(lines) if lines else "（未发现 Agent Definition）")
            return 0
        if action == "sidechains":
            lines = manager.sidechain_lines(limit=100)
            print("\n".join(lines) if lines else "（暂无 sidechain）")
            return 0
        if action == "sidechain":
            if not args.target:
                print("用法: krelcode agents sidechain <agent-id>", file=sys.stderr)
                return 2
            path, records = manager.read_sidechain(args.target)
            print(f"sidechain: {path}\nrecords: {len(records)}")
            return 0
        if action == "feed":
            if not args.target:
                print("用法: krelcode agents feed <agent-id> [--after N]", file=sys.stderr)
                return 2
            page = manager.feed(
                args.target, after_sequence=max(0, int(args.after or 0)), limit=500)
            print(page.render())
            return 0
        if action == "spawn":
            task = str(args.task or "").strip()
            if not task:
                print("agents spawn 需要 --task 或任务文本", file=sys.stderr)
                return 2
            handle = asyncio.run(manager.spawn(
                task,
                isolated=(True if args.isolated else None),
                fork_context=(True if args.fork_context else None),
                definition=args.definition,
            ))
            print(
                f"{handle.id} {handle.status}"
                + (f" branch={handle.branch}" if handle.branch else "")
                + f"\nsidechain: {handle.sidechain_path or '-'}")
            return 0
        if action == "wait":
            if not args.target:
                print("用法: krelcode agents wait <agent-id> [--wait 秒]", file=sys.stderr)
                return 2
            timeout = float(args.wait or 60.0)
            handle = asyncio.run(manager.wait(args.target, timeout=timeout))
            print(f"{handle.id} [{handle.status}] turns={handle.turns}\n{handle.preview()}")
            return 0
        if action in {"pause", "resume", "kill"}:
            if not args.target:
                print(f"用法: krelcode agents {action} <agent-id>", file=sys.stderr)
                return 2
            handle = asyncio.run(getattr(manager, action)(args.target))
            print(f"{handle.id} [{handle.status}]")
            return 0
        print("用法: krelcode agents list|spawn|wait|feed|pause|resume|kill|definitions|sidechains",
              file=sys.stderr)
        return 2
    except (DaemonUnavailable, RuntimeError, ValueError, KeyError, OSError) as exc:
        print(f"agents 错误: {exc}", file=sys.stderr)
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    raw_args = list(argv) if argv is not None else None
    effective = raw_args if raw_args is not None else list(sys.argv[1:])
    if effective and effective[0] == "benchmark":
        from krelcode.benchmarks.prompt import main as benchmark_main

        benchmark_args = effective[1:]
        if benchmark_args and benchmark_args[0] == "prompt":
            benchmark_args = benchmark_args[1:]
        return benchmark_main(benchmark_args)

    args = build_parser().parse_args(raw_args)
    if args.command == "doctor":
        return _run_doctor(args)
    if args.command == "init":
        return _run_init(args)
    if args.command == "daemon":
        return _run_daemon(args)
    if args.command == "jobs":
        return _run_jobs(args)
    if args.command == "agents":
        return _run_agents(args)

    try:
        if args.command == "review":
            from krelcode.review import run_review

            return asyncio.run(run_review(args))
        if args.command == "security-review":
            from krelcode.security_review.runner import run_security_review

            return asyncio.run(run_security_review(args))

        # Agent/Provider/MCP/Sandbox composition stays behind the full CLI
        # boundary and is loaded only for conversation/headless runs.
        from krelcode.app import async_main

        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        # asyncio.run() has already cancelled the task and cleaned resources.
        return 130
    except ConfigError as exc:
        print(f"配置错误: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
