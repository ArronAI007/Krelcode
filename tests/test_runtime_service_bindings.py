"""Composition-root bindings for redaction, policy and persistence services."""

from __future__ import annotations

import asyncio

from krelcode.app import build_loop, build_parser
from krelcode.bootstrap.composition import RuntimeServiceBinder


def test_runtime_service_binder_reaches_all_sensitive_boundaries(tmp_path):
    async def scenario():
        args = build_parser().parse_args([
            "--mock",
            "--workspace",
            str(tmp_path),
            "--no-context",
        ])
        loop, manager = await build_loop(args, {}, log=lambda _line: None)
        try:
            binder = loop.runtime_service_binder
            assert isinstance(binder, RuntimeServiceBinder)
            secret = "sk-runtime-binding-secret"
            binder.add_secret(secret)

            targets = (
                loop.context_manager,
                loop.rule_loader,
                loop.memory_store,
                loop.compact_checkpoint_store,
                loop.context_checkpoint_store,
                loop.context_checkpoint_coordinator,
                manager,
                manager.sidechains,
                loop.tool_context.shell_jobs,
            )
            assert all(target.redactor is binder.redactor for target in targets)
            assert loop.working_context_store.redactor.__self__ is binder.redactor
            assert loop.redactor is binder.redactor
            assert binder.redactor.redact(secret) == "***"

            services = loop.runtime_services
            assert loop.memory_store is services.persistence.memory_store
            assert loop.working_context_store is (
                services.persistence.working_context_store)
            assert loop.compact_checkpoint_store is (
                services.persistence.compact_checkpoint_store)
            assert loop.context_checkpoint_store is (
                services.persistence.context_checkpoint_store)
            assert loop.context_checkpoint_coordinator is (
                services.persistence.context_checkpoint_coordinator)
            assert manager.sidechains is services.persistence.sidechains
            assert loop.tool_context.shell_jobs is (
                services.persistence.shell_jobs)
            assert loop.auto_memory is services.persistence.auto_memory
        finally:
            await manager.shutdown()
            await loop.tool_context.shell_jobs.shutdown()

    asyncio.run(scenario())


def test_runtime_service_binder_is_idempotent_and_binds_policies(tmp_path):
    async def scenario():
        args = build_parser().parse_args([
            "--mock",
            "--workspace",
            str(tmp_path),
            "--no-context",
        ])
        loop, manager = await build_loop(args, {}, log=lambda _line: None)
        try:
            binder = loop.runtime_service_binder
            secret = "sk-idempotent-runtime-secret"
            binder.add_secret(secret)
            binder.add_secret(secret)

            identities = (
                id(loop.memory_store),
                id(loop.working_context_store),
                id(loop.context_checkpoint_store),
                id(loop.context_checkpoint_coordinator),
                id(loop.tool_context.shell_jobs),
            )
            binder.bind(loop=loop, subagents=manager)
            binder.bind(loop=loop, subagents=manager)

            assert identities == (
                id(loop.memory_store),
                id(loop.working_context_store),
                id(loop.context_checkpoint_store),
                id(loop.context_checkpoint_coordinator),
                id(loop.tool_context.shell_jobs),
            )
            assert binder.redactor.secrets.count(secret) == 1
            assert loop.policy is binder.services.approval_policy
            assert manager.policy is binder.services.approval_policy
            assert loop.enterprise_policy is binder.services.enterprise_policy
            assert loop.tool_context.enterprise_policy is (
                binder.services.enterprise_policy)
            assert manager.enterprise_policy is (
                binder.services.enterprise_policy)
            assert loop.principal is binder.services.principal
            assert loop.tool_context.principal is binder.services.principal
        finally:
            await manager.shutdown()
            await loop.tool_context.shell_jobs.shutdown()

    asyncio.run(scenario())
