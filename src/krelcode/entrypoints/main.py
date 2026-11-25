"""Complete Krelcode CLI: parser, command routing and REPL startup.

The lightweight ``krelcode.entrypoints.cli`` fast path imports this module only
when it is needed. Command-specific dependencies stay inside route functions;
the Agent composition root (``krelcode.app``) is imported exclusively for the
conversation runner.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Coroutine, cast

from krelcode.bootstrap.parser import build_parser
from krelcode.core.errors import ConfigError

if TYPE_CHECKING:
    from krelcode.config import ConfigBundle
    from krelcode.daemon.client import DaemonClient
    from krelcode.enterprise.auth import SSOConfig
    from krelcode.enterprise.policy import EnterprisePolicy



def _run_doctor(args: argparse.Namespace) -> int:
    from krelcode.config import (
        ConfigBundle,
        load_config_layers,
        managed_config_layer,
    )
    from krelcode.core.errors import ConfigError
    from krelcode.doctor import exit_code, format_report, run_all_checks

    config_path = Path(args.config).expanduser() if args.config else None
    try:
        bundle = load_config_layers(
            workspace=Path(args.workspace),
            explicit_path=config_path,
        )
        if config_path is not None:
            # Doctor must not let --config hide a malformed or restrictive
            # organization policy.  Keep the explicit data for provider
            # checks while adding managed provenance for enterprise checks.
            managed = managed_config_layer(Path(args.workspace))
            bundle = ConfigBundle(
                bundle.data, tuple(bundle.layers) + (managed,))
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


def _daemon_client(args: argparse.Namespace) -> "DaemonClient":
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
                from krelcode.bootstrap.composition.daemon import build_daemon_agent_runtime
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
                    agent_runtime_factory=build_daemon_agent_runtime,
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


def _load_enterprise_config(
    args: argparse.Namespace,
) -> tuple["ConfigBundle", "EnterprisePolicy"]:
    from krelcode.config import load_config_layers, load_managed_config
    from krelcode.enterprise.policy import load_enterprise_policy

    config_path = Path(args.config).expanduser() if args.config else None
    bundle = load_config_layers(
        workspace=Path(args.workspace),
        explicit_path=config_path,
    )
    if config_path is not None and not bundle.sources:
        raise ConfigError(f"指定的配置文件不存在: {config_path}")
    if config_path is not None:
        managed = load_managed_config(workspace=Path(args.workspace))
        policy = load_enterprise_policy(managed)
    else:
        policy = load_enterprise_policy(
            bundle.data, config_bundle=bundle)
    return bundle, policy


def _sso_config_for_args(
    args: argparse.Namespace,
    policy: "EnterprisePolicy",
) -> "SSOConfig":
    from krelcode.enterprise.auth import SSOConfig

    raw = dict(getattr(policy, "raw_sso", {}) or {})
    if args.issuer:
        raw["issuer"] = args.issuer
        raw["enabled"] = True
    if args.client_id:
        raw["client_id"] = args.client_id
    if args.redirect_uri:
        raw["redirect_uri"] = args.redirect_uri
    if args.token_dir:
        raw["token_dir"] = args.token_dir
    if args.allow_http_issuer:
        raw["allow_http_issuer"] = True
    return SSOConfig.from_mapping(
        raw, required=bool(getattr(policy, "require_sso", False)))


def _run_enterprise(args: argparse.Namespace) -> int:
    from krelcode.enterprise.audit import AuditConfig, verify_audit_file
    from krelcode.enterprise.auth import OIDCClient
    from krelcode.enterprise.policy import EnterprisePolicyError

    action = (args.action or "status").lower()
    if action != "status":
        print("用法: krelcode enterprise status", file=sys.stderr)
        return 2
    try:
        _bundle, policy = _load_enterprise_config(args)
        print(f"organization: {policy.organization or '-'}")
        print(f"enabled: {'yes' if policy.enabled else 'no'}")
        print(f"policy_version: {policy.policy_version or '-'}")
        print(f"max_permission_mode: {policy.max_permission_mode.value}")
        print(f"allow_auto_approve: {str(policy.allow_auto_approve).lower()}")
        print(f"allow_no_sandbox: {str(policy.allow_no_sandbox).lower()}")
        print(f"disabled_tools: {', '.join(policy.disabled_tools) or '-'}")
        if policy.require_sso:
            sso = OIDCClient(_sso_config_for_args(args, policy))
            status = sso.status()
            print(f"sso: {'authenticated' if status.authenticated else 'not-authenticated'}")
            if status.principal:
                print(f"principal: {status.principal.subject}")
        else:
            print("sso: not-required")
        audit = AuditConfig.from_mapping(
            policy.audit, workspace=Path(args.workspace))
        print(f"audit: {'enabled' if audit.enabled else 'disabled'}")
        print(f"audit_path: {audit.path}")
        if audit.enabled and audit.path.is_file():
            verification = verify_audit_file(audit.path)
            print(f"audit_chain: {'valid' if verification.valid else 'invalid'}"
                  f" records={verification.records}")
        return 0
    except (EnterprisePolicyError, ConfigError, OSError, ValueError) as exc:
        print(f"enterprise status 错误: {exc}", file=sys.stderr)
        return 2


def _run_audit(args: argparse.Namespace) -> int:
    from krelcode.enterprise.audit import AuditConfig, AuditError, verify_audit_file

    action = (args.action or "verify").lower()
    try:
        _bundle, policy = _load_enterprise_config(args)
        audit = AuditConfig.from_mapping(
            policy.audit, workspace=Path(args.workspace))
        path = Path(args.target).expanduser() if args.target else audit.path
        hmac_key = None
        if audit.mode == "hmac" and audit.hmac_key_env:
            value = os.environ.get(audit.hmac_key_env, "")
            if value:
                hmac_key = value.encode("utf-8")
        if action == "verify":
            result = verify_audit_file(path, hmac_key=hmac_key)
            print(f"path: {result.path}")
            print(f"valid: {'yes' if result.valid else 'no'}")
            print(f"records: {result.records}")
            print(f"last_sequence: {result.last_sequence}")
            if result.first_error:
                print(f"error: {result.first_error}", file=sys.stderr)
            return 0 if result.valid else 1
        if action == "export":
            if not args.audit_output:
                print("audit export 需要 --audit-output <路径>", file=sys.stderr)
                return 2
            source = path
            if not source.is_file():
                raise AuditError(f"审计文件不存在: {source}")
            destination = Path(args.audit_output).expanduser()
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())
            try:
                destination.chmod(0o600)
            except OSError:
                pass
            print(destination)
            return 0
        print("用法: krelcode audit verify|export [路径]", file=sys.stderr)
        return 2
    except (AuditError, ConfigError, OSError, ValueError) as exc:
        print(f"audit 错误: {exc}", file=sys.stderr)
        return 1


def _run_auth(args: argparse.Namespace) -> int:
    from krelcode.enterprise.auth import AuthError, OIDCClient
    from krelcode.enterprise.policy import EnterprisePolicyError

    action = (args.action or "status").lower()
    try:
        _bundle, policy = _load_enterprise_config(args)
        config = _sso_config_for_args(args, policy)
        if not config.enabled and not config.issuer:
            print("SSO 未配置；请在 managed [enterprise.sso] 中设置 issuer/client_id",
                  file=sys.stderr)
            return 2
        client = OIDCClient(config)
        if action == "status":
            status = client.status()
            print(f"authenticated: {'yes' if status.authenticated else 'no'}")
            print(f"source: {status.source}")
            if status.principal:
                print(f"subject: {status.principal.subject}")
                print(f"issuer: {status.principal.issuer}")
                print(f"method: {status.principal.auth_method}")
            if status.reason:
                print(f"reason: {status.reason}")
            return 0 if status.authenticated or not policy.require_sso else 1
        if action == "logout":
            print("SSO 已退出" if client.logout() else "没有 SSO 会话")
            return 0
        if action == "login":
            def show_url(url: str) -> None:
                print(f"authorization_url: {url}")

            tokens = asyncio.run(client.login(
                open_browser=not args.no_open_browser,
                authorization_url_callback=show_url,
            ))
            print(f"authenticated: yes")
            print(f"subject: {tokens.subject}")
            print(f"issuer: {tokens.issuer}")
            return 0
        print("用法: krelcode auth login|status|logout", file=sys.stderr)
        return 2
    except (AuthError, ConfigError, EnterprisePolicyError, OSError) as exc:
        print(f"auth 错误: {exc}", file=sys.stderr)
        return 1


def _run_telemetry(args: argparse.Namespace) -> int:
    from krelcode.telemetry import (
        TelemetryConfigError,
        TelemetryStoreError,
        build_telemetry_runtime,
    )

    action = (args.action or "status").strip().lower()
    try:
        bundle, policy = _load_enterprise_config(args)
        runtime = build_telemetry_runtime(
            bundle.data.get("telemetry"),
            provider="cli",
            model="cli",
            run_mode="cli",
            policy_allowed=policy.allow_telemetry,
        )
        if action == "status":
            status = runtime.status()
            print(f"mode: {status['mode']}")
            print(f"enabled: {'yes' if status['effective_enabled'] else 'no'}")
            print(f"policy_allowed: {'yes' if status['policy_allowed'] else 'no'}")
            print(f"sample_rate: {status['sample_rate']}")
            print(f"content_collection: never")
            print(f"endpoint_configured: {'yes' if status['endpoint_configured'] else 'no'}")
            if status["last_error"]:
                print(f"last_error: {status['last_error']}")
            queue = status["queue"]
            print(f"queue: {queue['events']} events / {queue['bytes']} bytes")
            print(f"queue_path: {queue['path']}")
            print(f"retention_days: {status['retention_days']}")
            return 0
        if action == "enable":
            mode = str(args.target or "local").strip().lower()
            runtime.enable(
                mode,
                endpoint=str(args.telemetry_endpoint or ""),
                sample_rate=args.telemetry_sample_rate,
            )
            print(f"telemetry: {runtime.config.mode}")
            print("content_collection: never")
            return 0
        if action == "disable":
            runtime.disable()
            print("telemetry: off")
            return 0
        if action == "summary":
            import json

            print(json.dumps(runtime.summary(), ensure_ascii=False, indent=2))
            return 0
        if action == "export":
            destination = args.telemetry_output or args.target
            if not destination:
                print("用法: krelcode telemetry export <path>", file=sys.stderr)
                return 2
            print(runtime.export(Path(destination).expanduser()))
            return 0
        if action == "clear":
            runtime.clear()
            print("telemetry queue cleared")
            return 0
        if action == "send":
            sent = asyncio.run(runtime.flush())
            if runtime.config.mode != "remote":
                print("telemetry mode 不是 remote；事件仅保留在本地队列")
                return 0
            print(f"sent: {sent} events")
            return 0 if not runtime.last_error else 1
        print(
            "用法: krelcode telemetry [status|enable [local|remote]|disable|"
            "summary|export <path>|clear|send]",
            file=sys.stderr,
        )
        return 2
    except (TelemetryConfigError, TelemetryStoreError, ConfigError, OSError) as exc:
        print(f"telemetry 错误: {exc}", file=sys.stderr)
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
    if args.command == "enterprise":
        return _run_enterprise(args)
    if args.command == "audit":
        return _run_audit(args)
    if args.command == "auth":
        return _run_auth(args)
    if args.command == "telemetry":
        return _run_telemetry(args)

    try:
        if args.command == "review":
            from krelcode.review import run_review

            return asyncio.run(run_review(args))
        if args.command == "security-review":
            from krelcode.security_review.runner import run_security_review

            return asyncio.run(run_security_review(args))

        # Agent/Provider/MCP/Sandbox composition stays behind the full CLI
        # boundary and is loaded only for conversation/headless runs.
        # Preserve the historical test/embedding seam when the compatibility
        # facade is already loaded, but do not create a static dependency back
        # to ``krelcode.app`` from the process entrypoint.
        import sys

        app_module = sys.modules.get("krelcode.app")
        if app_module is not None:
            async_main = app_module.async_main
        else:
            from krelcode.bootstrap.main import async_main

        entrypoint = cast(
            Callable[[argparse.Namespace], Coroutine[Any, Any, Any]], async_main)
        return int(asyncio.run(entrypoint(args)))
    except KeyboardInterrupt:
        # asyncio.run() has already cancelled the task and cleaned resources.
        return 130
    except ConfigError as exc:
        print(f"配置错误: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
