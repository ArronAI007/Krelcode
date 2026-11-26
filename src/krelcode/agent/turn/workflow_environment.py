"""Environment installation workflow definitions."""

from __future__ import annotations

from typing import Callable, TypeVar

_RuleT = TypeVar("_RuleT")
_DefinitionT = TypeVar("_DefinitionT")


def environment_setup_rules(
    task_type: object,
    rule_cls: Callable[..., _RuleT],
) -> tuple[_RuleT, ...]:
    return (
        rule_cls(
            task_type,
            "environment-setup-action",
            (
                "安装", "安装一下", "配置环境", "环境配置", "部署", "初始化环境",
                "install", "set up", "setup", "configure the environment",
                "environment setup",
            ),
            7,
        ),
        rule_cls(
            task_type,
            "environment-tooling-topic",
            (
                "hermes", "homebrew", "brew install", "pip install",
                "npm install", "uv tool", "package manager", "依赖安装",
                "全局命令", "installer", "toolchain", "runtime setup",
            ),
            2,
        ),
    )


def environment_setup_workflow(
    task_type: object,
    definition_cls: Callable[..., _DefinitionT],
) -> _DefinitionT:
    return definition_cls(
        task_type=task_type,
        id="environment-setup",
        title="Verified environment installation workflow",
        objective=(
            "Install or configure a tool at the user's requested scope with "
            "minimal commands and verified launchability."
        ),
        priority=15,
        steps=(
            "Establish the requested install scope, target command name, OS/architecture, and whether global or project-local installation is required.",
            "Fetch the authoritative install documentation once and select one supported installation path; avoid probing the same repository or system repeatedly.",
            "Do not hand a routine download or user-level install back to the user. Run it yourself: set sandbox_permission=user-install for installer downloads/scripts, preserve the app name in filenames such as hermes-install.sh, and then execute the reviewed installer.",
            "For a global command that is owned by the current user, target ~/.local/bin or ~/.local/share plus the app's inferred user home such as ~/.hermes. Never use sudo or system-wide locations such as /usr/local, /etc, /System, or /Library.",
            "State the required network, filesystem, package-manager, and privilege boundaries before executing.",
            "Use the smallest supported installer or isolated environment. Batch related setup operations and verify the resulting command path and version once.",
            "If a sandbox call reports network denied for an installer, retry once with sandbox_permission=user-install instead of asking the user to run it. Stop only if the user-install retry or managed policy still blocks the exact command.",
            "If the requested outcome cannot be achieved without a manual credential or user-only action, explain that limitation explicitly and identify the last verified completed step.",
        ),
        completion=(
            "The requested command is installed at the requested scope and resolves from the intended environment.",
            "Version and one meaningful smoke invocation are verified with tool evidence.",
            "Any manual follow-up or environment limitation is explicit and is not presented as a completed installation.",
        ),
        avoid=(
            "Do not repeatedly inspect, reinstall, or run doctor loops after one clear failure.",
            "Do not claim a global install when only a workspace-local launcher exists.",
            "Do not replace a permitted user-level install with --no-sandbox or a system-wide privileged install.",
            "Do not ask for API keys or fabricate a successful model/runtime smoke test when the user has not supplied credentials.",
        ),
    )
