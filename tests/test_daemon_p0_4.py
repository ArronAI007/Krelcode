"""P0-4: durable daemon records, file RPC, jobs and subagent control."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import time
from pathlib import Path

import pytest

from krelcode.daemon.client import DaemonClient, DaemonJobManager, DaemonSubagentManager
from krelcode.daemon.options import resolve_daemon_options
from krelcode.daemon.paths import (
    DaemonPaths,
    InstanceLock,
    atomic_write_json,
    probe_daemon,
    read_token,
)
from krelcode.daemon.protocol import (
    RPCRequest,
    RPCResponse,
    encode_message,
    ProtocolError,
)
from krelcode.daemon.server import DaemonServer
from krelcode.daemon.storage import (
    AgentRecordStore,
    DurableJobRecord,
    JobRecordStore,
)
from krelcode.daemon.transport import DaemonEndpoint
from krelcode.core.events import EventBus, ShellJobFinished, ShellJobOutput
from krelcode.entrypoints.main import build_parser
from krelcode.jobs import ShellJobStatus


def force_file_transport(monkeypatch):
    monkeypatch.setattr(
        "krelcode.daemon.server.choose_endpoint",
        lambda _paths: DaemonEndpoint("file"),
    )


async def wait_job(client: DaemonClient, job_id: str, timeout: float = 5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        rows = await client.request("job.list", {}, auto_start=True)
        for row in rows:
            if row["job_id"] == job_id:
                if row["status"] != ShellJobStatus.RUNNING.value:
                    return row
        await asyncio.sleep(0.03)
    raise AssertionError(f"job did not finish: {job_id}")


def test_protocol_roundtrip_and_size_guard():
    request = RPCRequest.create("ping", {"value": 1}, token="secret")
    parsed = RPCRequest.from_dict(request.to_dict())
    assert parsed == request
    response = RPCResponse.from_dict(
        RPCResponse.success(request.id, {"ok": True}).to_dict())
    assert response.ok and response.result == {"ok": True}
    with pytest.raises(ProtocolError, match="版本"):
        RPCRequest.from_dict({**request.to_dict(), "schema": 999})
    with pytest.raises(ProtocolError, match="大小"):
        encode_message({"value": "a" * 5_000_000})


def test_atomic_token_permissions_and_daemon_options(tmp_path, monkeypatch):
    paths = DaemonPaths(tmp_path).ensure()
    atomic_write_json(paths.state, {"instance_id": "x"})
    assert json.loads(paths.state.read_text())["instance_id"] == "x"
    if __import__("os").name == "posix":
        assert stat.S_IMODE(paths.state.stat().st_mode) == 0o600
    from krelcode.daemon.paths import secure_write_text

    secure_write_text(paths.token, "token\n")
    assert read_token(paths) == "token"
    if __import__("os").name == "posix":
        assert stat.S_IMODE(paths.token.stat().st_mode) == 0o600

    assert resolve_daemon_options(object(), {}).enabled is False
    assert resolve_daemon_options(
        object(), {"daemon": {"enabled": "auto"}}).enabled is True
    monkeypatch.setattr("krelcode.daemon.options.Path.home", lambda: tmp_path)
    assert resolve_daemon_options(
        object(), {"daemon": {"enabled": "off"}}).mode == "off"


def test_job_records_are_atomic_cursor_safe_and_redacted(tmp_path):
    paths = DaemonPaths(tmp_path).ensure()

    class Redactor:
        def redact(self, text):
            return str(text).replace("sk-secret", "***")

    store = JobRecordStore(paths, redactor=Redactor())
    record = DurableJobRecord(
        job_id="job-1",
        command="echo sk-secret",
        cwd=str(tmp_path),
        daemon_instance_id="daemon-a",
        started_at=1.0,
    )
    store.save(record)
    store.append_output("job-1", 1, "sk-secret\n")
    page = store.output("job-1", after_sequence=0)
    assert page is not None and page.text == "***\n"
    assert page.next_sequence == 1
    empty = store.output("job-1", after_sequence=page.next_sequence)
    assert empty is not None and empty.text == ""
    assert "sk-secret" not in next(paths.jobs_dir.glob("*.json")).read_text()

    store.mark_terminal("job-1", status="completed", exit_code=0)
    changed = store.reconcile("daemon-b")
    assert changed == []  # terminal jobs survive daemon replacement


def test_running_record_from_dead_daemon_is_lost_not_killed(tmp_path, monkeypatch):
    force_file_transport(monkeypatch)

    async def scenario():
        paths = DaemonPaths(tmp_path).ensure()
        store = JobRecordStore(paths)
        store.save(DurableJobRecord(
            job_id="job-old",
            command="sleep 30",
            cwd=str(tmp_path),
            pid=999_999,
            daemon_instance_id="dead-daemon",
            started_at=1.0,
        ))
        server = DaemonServer(tmp_path, idle_timeout=0, no_sandbox=True, log=lambda _x: None)
        await server.start()
        client = DaemonClient(tmp_path)
        try:
            rows = await client.request("job.list", {})
            assert rows[0]["status"] == ShellJobStatus.LOST.value
            with pytest.raises(Exception, match="无法安全"):
                await client.request("job.kill", {"job_id": "job-old"})
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_lock_probe_reclaims_reused_pid_without_a_live_instance(tmp_path, monkeypatch):
    force_file_transport(monkeypatch)
    paths = DaemonPaths(tmp_path).ensure()
    atomic_write_json(paths.state, {
        "workspace_root": str(tmp_path),
        "instance_id": "dead-instance",
        "endpoint": {"kind": "file"},
    })
    from krelcode.daemon.paths import secure_write_text

    secure_write_text(paths.token, "dead-token\n")
    atomic_write_json(paths.lock, {
        "workspace_root": str(tmp_path),
        "instance_id": "dead-instance",
        "pid": os.getpid(),  # PID remains alive, but it is not the daemon.
        "created_at": time.time() - 11.0,
    })
    assert probe_daemon(paths) is False
    lock = InstanceLock(paths)
    lock.acquire({"workspace_root": str(tmp_path), "instance_id": "new-instance"})
    try:
        assert json.loads(paths.lock.read_text())["instance_id"] == "new-instance"
    finally:
        lock.release()


def test_daemon_agent_runtime_uses_mcp_supervisor(tmp_path, monkeypatch):
    class FakeSupervisor:
        instances = []

        def __init__(self, registry, **kwargs):
            self.registry = registry
            self.kwargs = kwargs
            self.servers = {}
            self.closed = False
            self.instances.append(self)

        async def sync(self, servers):
            self.servers = dict(servers)

        async def close_all(self):
            self.closed = True

        def server_instructions(self):
            return ()

    monkeypatch.setattr("krelcode.mcp.supervisor.MCPSupervisor", FakeSupervisor)

    async def scenario():
        server = DaemonServer(
            tmp_path,
            config={
                "default_provider": "mock",
                "mcp_servers": {"demo": {"command": "true"}},
            },
            idle_timeout=0,
            no_sandbox=True,
            log=lambda _text: None,
        )
        await server.start()
        try:
            loop, _agents = await server._ensure_agent_runtime()
            supervisor = FakeSupervisor.instances[-1]
            assert supervisor.servers == {"demo": {"command": "true"}}
            assert loop.mcp_supervisor is supervisor
        finally:
            await server.stop()
        assert supervisor.closed is True

    asyncio.run(scenario())


def test_file_rpc_job_survives_client_disconnect_and_daemon_restart(tmp_path, monkeypatch):
    force_file_transport(monkeypatch)

    async def scenario():
        server = DaemonServer(tmp_path, idle_timeout=0, no_sandbox=True, log=lambda _x: None)
        await server.start()
        client = DaemonClient(tmp_path)
        bus = EventBus()
        events = []
        bus.subscribe(events.append)
        manager = DaemonJobManager(tmp_path, client=client, bus=bus)
        try:
            job = await manager.start(
                "printf 'one\\n'; sleep 0.1; printf 'two\\n'", cwd=tmp_path)
            final = await wait_job(client, job.job_id)
            assert final["status"] == "completed" and final["exit_code"] == 0
            first = await manager.output(job.job_id)
            assert "one" in first.text and "two" in first.text
            rest = await manager.output(
                job.job_id, after_sequence=first.next_sequence)
            assert rest.text == ""
            sync_rows = await asyncio.to_thread(
                client._request_sync, "job.list", {})
            assert sync_rows[0]["job_id"] == job.job_id
            await asyncio.sleep(0.7)
            assert any(isinstance(event, ShellJobOutput) for event in events)
            assert any(isinstance(event, ShellJobFinished) for event in events)
        finally:
            await manager.shutdown()
            await server.stop()

        # A new daemon reconciles metadata rather than deleting completed work.
        restarted = DaemonServer(
            tmp_path, idle_timeout=0, no_sandbox=True, log=lambda _x: None)
        await restarted.start()
        try:
            client2 = DaemonClient(tmp_path)
            rows = await client2.request("job.list", {})
            assert rows[0]["job_id"] == "job-1"
            page = await client2.request(
                "job.output", {"job_id": "job-1", "after_sequence": 0})
            assert "one" in page["text"] and "two" in page["text"]
            assert page["job"]["status"] == "completed"
        finally:
            await restarted.stop()

    asyncio.run(scenario())


def test_subagent_daemon_spawn_wait_and_restart_audit(tmp_path, monkeypatch):
    force_file_transport(monkeypatch)
    (tmp_path / "krelcode.toml").write_text(
        'default_provider = "mock"\n', encoding="utf-8")

    async def scenario():
        server = DaemonServer(
            tmp_path,
            config={"default_provider": "mock"},
            idle_timeout=0,
            no_sandbox=True,
            log=lambda _x: None,
        )
        await server.start()
        client = DaemonClient(tmp_path)
        manager = DaemonSubagentManager(tmp_path, client=client)
        try:
            handle = await manager.spawn("say hello", definition="general-purpose")
            finished = await manager.wait(handle.id, timeout=8)
            assert finished.status == "completed"
            assert "mock" in (finished.result or "").lower()
            rows = await client.request("agent.list", {})
            assert rows[0]["agent_id"] == handle.id
            assert rows[0]["status"] == "completed"
        finally:
            await server.stop()

        restarted = DaemonServer(
            tmp_path,
            config={"default_provider": "mock"},
            idle_timeout=0,
            no_sandbox=True,
            log=lambda _x: None,
        )
        await restarted.start()
        try:
            client2 = DaemonClient(tmp_path)
            rows = await client2.request("agent.list", {})
            assert rows[0]["agent_id"] == "a1"
            assert rows[0]["status"] == "completed"
            feed = await client2.request(
                "agent.feed", {"agent_id": "a1", "after_sequence": 0})
            assert feed["latest_sequence"] >= 1
            assert any(item["kind"] == "run_finished" for item in feed["entries"])
        finally:
            await restarted.stop()

    asyncio.run(scenario())


def test_cli_parses_daemon_job_and_agent_commands(tmp_path):
    parser = build_parser()
    jobs = parser.parse_args([
        "jobs", "start", "--command", "sleep 30", "--workspace", str(tmp_path)])
    assert jobs.command == "jobs" and jobs.action == "start"
    assert jobs.job_command == "sleep 30"
    agent = parser.parse_args([
        "agents", "spawn", "--task", "review code", "--workspace", str(tmp_path)])
    assert agent.command == "agents" and agent.task == "review code"
    daemon = parser.parse_args([
        "daemon", "start", "--daemon-idle-timeout", "0", "--workspace", str(tmp_path)])
    assert daemon.command == "daemon" and daemon.action == "start"
