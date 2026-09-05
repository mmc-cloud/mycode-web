import asyncio
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, call

import pytest

from app.config import ServerSettings
from app.services.events import EventHub
from app.services.runtime import (
    AGENT_USER,
    DockerSandboxLauncher,
    MANAGED_SANDBOX_LABEL,
    RuntimeCapacityError,
    RuntimeConflictError,
    RuntimeManager,
    RuntimeUnavailableError,
    _normalize_cli_message,
)
from app.services.workspace import WorkspaceService


SCOPED_PERMISSION_PROMPT = (
    "是否批准？[y/yes 本次 | t/task 当前任务 | "
    "s/session 当前会话 | N 拒绝] "
)


class FakeStdout:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def read(self, size: int = -1) -> bytes:
        return await self.queue.get()

    async def feed(self, content: bytes) -> None:
        await self.queue.put(content)


class FakeStdin:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None


class FakeProcess:
    def __init__(self) -> None:
        self.stdin = FakeStdin()
        self.stdout = FakeStdout()
        self.returncode = None
        self.done = asyncio.Event()

    async def wait(self) -> int:
        await self.done.wait()
        return self.returncode or 0

    def terminate(self) -> None:
        self.returncode = 0
        self.done.set()
        self.stdout.queue.put_nowait(b"")

    def kill(self) -> None:
        self.terminate()


class FakeLauncher:
    def __init__(self) -> None:
        self.processes: list[FakeProcess] = []
        self.by_session: dict[str, list[FakeProcess]] = {}
        self.calls = 0
        self.max_live_seen = 0

    @property
    def process(self) -> FakeProcess:
        return self.processes[-1]

    async def launch(self, session_id: str, workspace: Path, mycode_state: Path):
        self.calls += 1
        process = FakeProcess()
        self.processes.append(process)
        self.by_session.setdefault(session_id, []).append(process)
        self.max_live_seen = max(
            self.max_live_seen,
            sum(candidate.returncode is None for candidate in self.processes),
        )
        asyncio.get_running_loop().call_soon(
            process.stdout.queue.put_nowait, b"started\nyou> "
        )
        return process


class ManualPromptLauncher(FakeLauncher):
    async def launch(self, session_id: str, workspace: Path, mycode_state: Path):
        self.calls += 1
        process = FakeProcess()
        self.processes.append(process)
        self.by_session.setdefault(session_id, []).append(process)
        return process


class FailNextLauncher(FakeLauncher):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next = False

    async def launch(self, session_id: str, workspace: Path, mycode_state: Path):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulated startup failure")
        return await super().launch(session_id, workspace, mycode_state)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def wait_for_status(
    manager: RuntimeManager, session_id: str, expected: str
) -> None:
    for _ in range(100):
        if manager.status(session_id) == expected:
            return
        await asyncio.sleep(0)
    raise AssertionError(
        f"{session_id} remained {manager.status(session_id)!r}, expected {expected!r}"
    )


def settings(tmp_path: Path) -> ServerSettings:
    return ServerSettings(
        data_dir=tmp_path / "data",
        provider_api_key="real-provider-secret",
        model="demo-model",
        sandbox_optional_env=(),
    )


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("check app.py", "check app.py"),
        ("check app.py\nthen run pytest", "check app.py then run pytest"),
        ("check app.py\r\nthen run pytest", "check app.py then run pytest"),
        (
            "check app.py\n\n  \n\tthen run pytest",
            "check app.py then run pytest",
        ),
    ],
)
def test_cli_message_normalization(message: str, expected: str) -> None:
    assert _normalize_cli_message(message) == expected


def test_runtime_writes_multiline_browser_message_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = settings(tmp_path)
        launcher = FakeLauncher()
        manager = RuntimeManager(
            config,
            WorkspaceService(config),
            EventHub(),
            launcher=launcher,
        )

        await manager.send_message(
            "session",
            "check app.py\r\n\r\n  then run pytest",
        )

        assert launcher.process.stdin.writes == [
            b"check app.py then run pytest\n"
        ]
        await manager.shutdown()

    asyncio.run(scenario())


def test_same_user_runtimes_share_workspace_but_keep_state_private(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        config = replace(settings(tmp_path), sandbox_max_active=2)
        owners = {"session-a": "user-a", "session-b": "user-a"}
        launch_paths: list[tuple[Path, Path]] = []

        class RecordingLauncher(FakeLauncher):
            async def launch(
                self, session_id: str, workspace: Path, mycode_state: Path
            ):
                launch_paths.append((workspace, mycode_state))
                return await super().launch(session_id, workspace, mycode_state)

        manager = RuntimeManager(
            config,
            WorkspaceService(config),
            EventHub(),
            launcher=RecordingLauncher(),
            session_owner_resolver=owners.get,
        )

        assert await manager.send_message("session-a", "one") == "running"
        assert await manager.send_message("session-b", "two") == "running"
        assert launch_paths[0][0] == launch_paths[1][0]
        assert launch_paths[0][1] != launch_paths[1][1]
        await manager.shutdown()

    asyncio.run(scenario())


def test_activate_is_async_idempotent_and_message_waits_same_runtime(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        config = settings(tmp_path)
        launcher = ManualPromptLauncher()
        manager = RuntimeManager(
            config, WorkspaceService(config), EventHub(), launcher=launcher
        )

        assert await manager.activate("session") == "starting"
        assert await manager.activate("session") == "starting"
        await asyncio.sleep(0)
        assert launcher.calls == 1
        send = asyncio.create_task(manager.send_message("session", "hello"))
        await asyncio.sleep(0)
        assert not send.done()
        await launcher.process.stdout.feed(b"started\nyou> ")
        assert await asyncio.wait_for(send, timeout=1) == "running"
        assert launcher.calls == 1
        assert launcher.process.stdin.writes == [b"hello\n"]
        await manager.shutdown()

    asyncio.run(scenario())


def test_sse_reconnect_and_repeated_open_do_not_restart_runtime(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        config = settings(tmp_path)
        events = EventHub()
        launcher = FakeLauncher()
        manager = RuntimeManager(
            config, WorkspaceService(config), events, launcher=launcher
        )
        assert await manager.activate("session") == "starting"
        await wait_for_status(manager, "session", "idle")
        first_stream = events.stream("session", after_id=10_000)
        pending = asyncio.create_task(anext(first_stream))
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await first_stream.aclose()
        second_stream = events.stream("session", after_id=10_000)
        second_pending = asyncio.create_task(anext(second_stream))
        await asyncio.sleep(0)
        assert await manager.activate("session") == "idle"
        assert launcher.calls == 1
        second_pending.cancel()
        await asyncio.gather(second_pending, return_exceptions=True)
        await second_stream.aclose()
        await manager.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("return_code", "expected_status", "expects_error"),
    [(0, "stopped", False), (17, "error", True)],
)
def test_process_exit_semantics(
    tmp_path: Path, return_code: int, expected_status: str, expects_error: bool
) -> None:
    async def scenario() -> None:
        config = settings(tmp_path)
        events = EventHub()
        launcher = FakeLauncher()
        manager = RuntimeManager(
            config, WorkspaceService(config), events, launcher=launcher
        )
        await manager.activate("session")
        await wait_for_status(manager, "session", "idle")
        launcher.process.returncode = return_code
        launcher.process.done.set()
        await launcher.process.stdout.feed(b"")
        await wait_for_status(manager, "session", expected_status)
        errors = [event for event in events.history("session") if event.type == "error"]
        assert bool(errors) is expects_error
        await manager.shutdown()

    asyncio.run(scenario())


def test_cli_message_normalization_rejects_whitespace_only() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        _normalize_cli_message(" \t\r\n  \n")


def test_runtime_reuses_process_and_maps_permission_to_stdin(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = settings(tmp_path)
        workspace = WorkspaceService(config)
        events = EventHub()
        launcher = FakeLauncher()
        manager = RuntimeManager(config, workspace, events, launcher=launcher)

        await manager.send_message("session", "first task")
        assert launcher.process.stdin.writes == [b"first task\n"]
        with pytest.raises(RuntimeConflictError):
            await manager.send_message("session", "overlapping task")
        await launcher.process.stdout.feed(b"answer\nyou> ")
        await asyncio.sleep(0)
        await manager.send_message("session", "second task")
        assert launcher.calls == 1

        await launcher.process.stdout.feed(
            f"permission> run_command 需要确认\nreason> risky\n{SCOPED_PERMISSION_PROMPT}".encode()
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await manager.resolve_permission("session", "once")
        assert launcher.process.stdin.writes[-1] == b"y\n"
        assert manager.status("session") == "running"
        resolved = [
            event
            for event in events.history("session")
            if event.type == "permission_resolved"
        ][-1]
        assert resolved.data["decision"] == "once"
        assert resolved.data["allowed"] is True
        assert [event.type for event in events.history("session")][-2:] == [
            "permission_resolved",
            "runtime_status",
        ]
        assert events.history("session")[-1].data == {"status": "running"}
        await launcher.process.stdout.feed(
            f"permission> write_file 需要确认\nreason> write\n{SCOPED_PERMISSION_PROMPT}".encode()
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await manager.resolve_permission("session", "deny")
        assert launcher.process.stdin.writes[-1] == b"n\n"
        assert manager.status("session") == "running"
        resolved = [
            event
            for event in events.history("session")
            if event.type == "permission_resolved"
        ][-1]
        assert resolved.data["decision"] == "deny"
        assert resolved.data["allowed"] is False
        assert [event.type for event in events.history("session")][-2:] == [
            "permission_resolved",
            "runtime_status",
        ]
        await launcher.process.stdout.feed(
            f"permission> run_command 需要确认\nreason> task\n{SCOPED_PERMISSION_PROMPT}".encode()
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await manager.resolve_permission("session", "task")
        assert launcher.process.stdin.writes[-1] == b"t\n"
        resolved = [
            event
            for event in events.history("session")
            if event.type == "permission_resolved"
        ][-1]
        assert resolved.data["decision"] == "task"
        assert resolved.data["allowed"] is True
        await launcher.process.stdout.feed(
            f"permission> run_command 需要确认\nreason> session\n{SCOPED_PERMISSION_PROMPT}".encode()
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await manager.resolve_permission("session", "session")
        assert launcher.process.stdin.writes[-1] == b"s\n"
        resolved = [
            event
            for event in events.history("session")
            if event.type == "permission_resolved"
        ][-1]
        assert resolved.data["decision"] == "session"
        assert resolved.data["allowed"] is True
        assert any(event.type == "permission_request" for event in events.history("session"))
        assert any(event.type == "permission_resolved" for event in events.history("session"))
        await manager.shutdown()

    asyncio.run(scenario())


def test_permission_is_strictly_bound_to_its_session(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = settings(tmp_path)
        launcher = FakeLauncher()
        manager = RuntimeManager(
            config, WorkspaceService(config), EventHub(), launcher=launcher
        )
        await manager.send_message("a", "task a")
        await manager.send_message("b", "task b")
        await launcher.by_session["a"][0].stdout.feed(
            f"permission> write_file 需要确认\n{SCOPED_PERMISSION_PROMPT}".encode()
        )
        await wait_for_status(manager, "a", "waiting_permission")
        assert manager.pending_permission("a") is not None
        assert manager.pending_permission("b") is None
        with pytest.raises(RuntimeConflictError, match="no pending permission"):
            await manager.resolve_permission("b", "once")
        assert launcher.by_session["b"][0].stdin.writes == [b"task b\n"]
        await manager.resolve_permission("a", "deny")
        assert launcher.by_session["a"][0].stdin.writes[-1] == b"n\n"
        await manager.shutdown()

    asyncio.run(scenario())


def test_docker_command_mounts_only_session_and_hides_provider_key(tmp_path: Path) -> None:
    config = settings(tmp_path)
    launcher = DockerSandboxLauncher(config)
    launcher.set_runtime_token("session", "runtime-token")
    command = launcher.command(
        "session", tmp_path / "workspace", tmp_path / "mycode_state"
    )
    rendered = " ".join(str(part) for part in command)
    assert "target=/workspace" in rendered
    assert "target=/home/mycode/.mycode" in rendered
    assert "MYCODE_API_KEY=runtime-token" in rendered
    assert "MYCODE_API_KEY=relay-token" not in rendered
    assert "MYCODE_BASE_URL=" in rendered
    assert "real-provider-secret" not in rendered
    assert f"--user {AGENT_USER}" in rendered
    assert "--cap-drop ALL" in rendered
    assert f"--label {MANAGED_SANDBOX_LABEL}" in rendered
    assert "--label mycode-web.session=session" in rendered


def test_startup_cleanup_selects_only_managed_containers(tmp_path: Path) -> None:
    async def scenario() -> None:
        launcher = DockerSandboxLauncher(settings(tmp_path))
        launcher._run_docker = AsyncMock(
            side_effect=[
                (0, "managed-one\nmanaged-two\n", ""),
                (0, "", ""),
            ]
        )

        selected = await launcher.cleanup_orphans()

        assert selected == ("managed-one", "managed-two")
        assert launcher._run_docker.await_args_list == [
            call("ps", "-aq", "--filter", "label=mycode-web.managed=true"),
            call("rm", "-f", "managed-one", "managed-two"),
        ]
        assert "syncthing" not in repr(launcher._run_docker.await_args_list)

    asyncio.run(scenario())


def test_startup_cleanup_list_failure_logs_and_touches_nothing(
    tmp_path: Path, caplog
) -> None:
    async def scenario() -> None:
        launcher = DockerSandboxLauncher(settings(tmp_path))
        launcher._run_docker = AsyncMock(
            return_value=(1, "", "Docker daemon unavailable")
        )

        assert await launcher.cleanup_orphans() == ()
        launcher._run_docker.assert_awaited_once_with(
            "ps", "-aq", "--filter", "label=mycode-web.managed=true"
        )

    caplog.set_level("WARNING")
    asyncio.run(scenario())
    assert "Docker daemon unavailable" in caplog.text


def test_docker_command_passes_only_configured_optional_mycode_env(
    tmp_path: Path,
) -> None:
    config = ServerSettings(
        data_dir=tmp_path / "data",
        provider_api_key="real-provider-secret",
        model="demo-model",
        sandbox_optional_env=(
            ("MYCODE_COMPACT_MODEL", "compact-model"),
            ("LLM_CONTEXT_WINDOW_TOKENS", "64000"),
            ("LLM_STREAM_INCLUDE_USAGE", "false"),
            ("LLM_THINKING_ENABLED", "true"),
            ("LLM_REASONING_EFFORT", "high"),
            ("LLM_MAX_OUTPUT_TOKENS", "4096"),
        ),
    )
    launcher = DockerSandboxLauncher(config)
    launcher.set_runtime_token("session", "runtime-token")
    rendered = " ".join(
        launcher.command("session", tmp_path / "workspace", tmp_path / "mycode_state")
    )
    assert "MYCODE_COMPACT_MODEL=compact-model" in rendered
    assert "LLM_CONTEXT_WINDOW_TOKENS=64000" in rendered
    assert "LLM_STREAM_INCLUDE_USAGE=false" in rendered
    assert "LLM_THINKING_ENABLED=true" in rendered
    assert "LLM_REASONING_EFFORT=high" in rendered
    assert "LLM_MAX_OUTPUT_TOKENS=4096" in rendered
    assert "LLM_RESERVED_OUTPUT_TOKENS=" not in rendered
    assert "MYCODE_PROVIDER_API_KEY" not in rendered
    assert "real-provider-secret" not in rendered


def test_docker_command_includes_configured_resource_limits(tmp_path: Path) -> None:
    config = ServerSettings(
        data_dir=tmp_path / "data",
        sandbox_memory_limit="768m",
        sandbox_memory_swap_limit="1200m",
        sandbox_cpus=1.5,
        sandbox_pids_limit=300,
    )
    launcher = DockerSandboxLauncher(config)
    launcher.set_runtime_token("session", "runtime-token")
    command = launcher.command(
        "session", tmp_path / "workspace", tmp_path / "state"
    )

    assert command[command.index("--memory") + 1] == "768m"
    assert command[command.index("--memory-swap") + 1] == "1200m"
    assert command[command.index("--cpus") + 1] == "1.5"
    assert command[command.index("--pids-limit") + 1] == "300"


def test_two_running_sessions_admit_and_third_queues_fifo(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = settings(tmp_path)
        launcher = FakeLauncher()
        events = EventHub()
        manager = RuntimeManager(
            config, WorkspaceService(config), events, launcher=launcher
        )

        assert await manager.send_message("one", "first") == "running"
        assert await manager.send_message("two", "second") == "running"
        assert await manager.send_message("three", "third\nline") == "queued"
        assert await manager.send_message("four", "fourth") == "queued"
        assert manager.active_count == 2
        assert manager.queued_count == 2

        await launcher.by_session["one"][0].stdout.feed(b"done\nyou> ")
        await wait_for_status(manager, "three", "running")
        assert manager.status("four") == "queued"
        assert launcher.by_session["three"][0].stdin.writes == [b"third line\n"]
        three_statuses = [
            event.data["status"]
            for event in events.history("three")
            if event.type == "runtime_status"
        ]
        assert three_statuses == ["queued", "starting", "running"]

        await launcher.by_session["two"][0].stdout.feed(b"done\nyou> ")
        await wait_for_status(manager, "four", "running")
        assert launcher.by_session["four"][0].stdin.writes == [b"fourth\n"]
        assert manager.active_count <= config.sandbox_max_active
        await manager.shutdown()

    asyncio.run(scenario())


def test_per_user_active_quota_does_not_block_another_user(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = replace(
            settings(tmp_path), sandbox_max_active=10, sandbox_max_active_per_user=2
        )
        owners = {"a1": "user-a", "a2": "user-a", "a3": "user-a", "b1": "user-b"}
        launcher = FakeLauncher()
        manager = RuntimeManager(
            config,
            WorkspaceService(config),
            EventHub(),
            launcher=launcher,
            session_owner_resolver=owners.get,
        )

        assert await manager.send_message("a1", "one") == "running"
        assert await manager.send_message("a2", "two") == "running"
        assert await manager.send_message("a3", "three") == "queued"
        assert await manager.send_message("b1", "one") == "running"
        assert manager.active_count == 3
        assert manager.queued_count == 1
        await manager.shutdown()

    asyncio.run(scenario())


def test_per_user_quota_release_handoffs_queued_session(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = replace(
            settings(tmp_path), sandbox_max_active=2, sandbox_max_active_per_user=2
        )
        owners = {"a1": "user-a", "a2": "user-a", "a3": "user-a"}
        launcher = FakeLauncher()
        manager = RuntimeManager(
            config,
            WorkspaceService(config),
            EventHub(),
            launcher=launcher,
            session_owner_resolver=owners.get,
        )

        await manager.send_message("a1", "one")
        await manager.send_message("a2", "two")
        assert await manager.send_message("a3", "three") == "queued"
        await manager.stop_session("a1")
        await wait_for_status(manager, "a3", "running")
        assert manager.status("a2") == "running"
        assert manager.queued_count == 0
        await manager.shutdown()

    asyncio.run(scenario())


def test_idle_handoff_releases_victim_user_slot_before_eligibility_check(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        config = replace(
            settings(tmp_path), sandbox_max_active=10, sandbox_max_active_per_user=2
        )
        owners = {"a1": "user-a", "a2": "user-a", "a3": "user-a"}
        launcher = FakeLauncher()
        manager = RuntimeManager(
            config,
            WorkspaceService(config),
            EventHub(),
            launcher=launcher,
            session_owner_resolver=owners.get,
        )

        await manager.send_message("a1", "one")
        await manager.send_message("a2", "two")
        assert await manager.send_message("a3", "three") == "queued"

        await launcher.by_session["a1"][0].stdout.feed(b"done\nyou> ")
        await wait_for_status(manager, "a3", "running")
        assert manager.status("a1") == "stopped"
        assert manager.status("a2") == "running"
        assert manager.queued_count == 0
        await manager.shutdown()

    asyncio.run(scenario())


def test_queue_skips_user_quota_head_of_line_blocker(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = replace(
            settings(tmp_path), sandbox_max_active=3, sandbox_max_active_per_user=2
        )
        owners = {
            "a1": "user-a", "a2": "user-a", "a3": "user-a",
            "b1": "user-b", "b2": "user-b",
        }
        launcher = FakeLauncher()
        manager = RuntimeManager(
            config,
            WorkspaceService(config),
            EventHub(),
            launcher=launcher,
            session_owner_resolver=owners.get,
        )

        await manager.send_message("a1", "one")
        await manager.send_message("a2", "two")
        await manager.send_message("b1", "one")
        assert await manager.send_message("a3", "three") == "queued"
        assert await manager.send_message("b2", "two") == "queued"

        await manager.stop_session("b1")
        await wait_for_status(manager, "b2", "running")
        assert manager.status("a3") == "queued"
        assert manager.queued_count == 1
        await manager.shutdown()

    asyncio.run(scenario())


def test_repeated_activate_and_terminal_leases_count_one_sandbox(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        config = replace(settings(tmp_path), sandbox_max_active_per_user=1)
        owners = {"session": "user-a"}
        launcher = FakeLauncher()
        manager = RuntimeManager(
            config,
            WorkspaceService(config),
            EventHub(),
            launcher=launcher,
            session_owner_resolver=owners.get,
        )

        assert await manager.activate("session") == "starting"
        assert await manager.activate("session") == "starting"
        await wait_for_status(manager, "session", "idle")
        await manager.acquire_terminal_lease("session")
        await manager.acquire_terminal_lease("session")
        assert await manager.activate("session") == "idle"
        assert await manager.send_message("session", "task") == "running"
        assert manager.active_count == 1
        assert launcher.calls == 1
        await manager.shutdown()

    asyncio.run(scenario())


def test_concurrent_same_user_admission_respects_per_user_limit(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        config = replace(
            settings(tmp_path), sandbox_max_active=10, sandbox_max_active_per_user=2
        )
        owners = {f"session-{index}": "user-a" for index in range(8)}
        launcher = FakeLauncher()
        manager = RuntimeManager(
            config,
            WorkspaceService(config),
            EventHub(),
            launcher=launcher,
            session_owner_resolver=owners.get,
        )

        results = await asyncio.gather(
            *(manager.send_message(session_id, "task") for session_id in owners)
        )

        assert results.count("running") == 2
        assert results.count("queued") == 6
        assert manager.active_count == 2
        assert launcher.calls == 2
        await manager.shutdown()

    asyncio.run(scenario())


def test_startup_failure_releases_quota_and_allows_next_queue_item(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        config = replace(
            settings(tmp_path), sandbox_max_active=1, sandbox_max_active_per_user=1
        )
        owners = {"a1": "user-a", "a2": "user-a", "b1": "user-b"}
        launcher = FailNextLauncher()
        manager = RuntimeManager(
            config,
            WorkspaceService(config),
            EventHub(),
            launcher=launcher,
            session_owner_resolver=owners.get,
        )

        await manager.send_message("a1", "one")
        assert await manager.send_message("a2", "two") == "queued"
        assert await manager.send_message("b1", "one") == "queued"
        launcher.fail_next = True
        await manager.stop_session("a1")
        await wait_for_status(manager, "b1", "running")
        assert manager.status("a2") == "error"
        assert manager.active_count == 1
        await manager.shutdown()

    asyncio.run(scenario())


def test_idle_ttl_releases_per_user_quota_for_queued_session(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock = FakeClock()
        config = replace(
            settings(tmp_path),
            sandbox_max_active=2,
            sandbox_max_active_per_user=1,
            sandbox_idle_ttl_seconds=10,
        )
        owners = {"a1": "user-a", "a2": "user-a"}
        launcher = FakeLauncher()
        manager = RuntimeManager(
            config,
            WorkspaceService(config),
            EventHub(),
            launcher=launcher,
            clock=clock,
            session_owner_resolver=owners.get,
        )

        await manager.send_message("a1", "one")
        await launcher.process.stdout.feed(b"done\nyou> ")
        await wait_for_status(manager, "a1", "idle")
        assert await manager.send_message("a2", "two") == "queued"
        clock.advance(10)
        assert await manager.sweep_expired() == ("a1",)
        await wait_for_status(manager, "a2", "running")
        await manager.shutdown()

    asyncio.run(scenario())


def test_oldest_idle_is_evicted_instead_of_queueing(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock = FakeClock()
        config = settings(tmp_path)
        workspace = WorkspaceService(config)
        launcher = FakeLauncher()
        start_watcher = AsyncMock()
        stop_watcher = AsyncMock()
        manager = RuntimeManager(
            config,
            workspace,
            EventHub(),
            launcher=launcher,
            clock=clock,
            runtime_start_hook=start_watcher,
            runtime_stop_hook=stop_watcher,
        )

        await manager.send_message("old-idle", "first")
        marker_workspace, marker_state = workspace.ensure_session_directories(
            "old-idle"
        )
        (marker_workspace / "keep.txt").write_text("workspace", encoding="utf-8")
        (marker_state / "keep.txt").write_text("state", encoding="utf-8")
        await launcher.by_session["old-idle"][0].stdout.feed(b"done\nyou> ")
        await wait_for_status(manager, "old-idle", "idle")
        clock.advance(10)
        await manager.send_message("busy", "second")

        assert await manager.send_message("new", "third") == "running"
        assert manager.status("old-idle") == "stopped"
        stop_watcher.assert_any_await("old-idle")
        assert manager.queued_count == 0
        assert (marker_workspace / "keep.txt").read_text(encoding="utf-8") == "workspace"
        assert (marker_state / "keep.txt").read_text(encoding="utf-8") == "state"
        await manager.shutdown()

    asyncio.run(scenario())


def test_duplicate_queue_entry_and_queue_capacity_are_rejected(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        config = ServerSettings(
            data_dir=tmp_path / "data",
            sandbox_max_active=2,
            sandbox_queue_max=1,
        )
        launcher = FakeLauncher()
        manager = RuntimeManager(
            config, WorkspaceService(config), EventHub(), launcher=launcher
        )
        await manager.send_message("one", "first")
        await manager.send_message("two", "second")
        await manager.send_message("three", "third")

        with pytest.raises(RuntimeConflictError, match="active or queued"):
            await manager.send_message("three", "duplicate")
        with pytest.raises(RuntimeCapacityError, match="queue is full"):
            await manager.send_message("four", "overflow")
        await manager.shutdown()

    asyncio.run(scenario())


def test_idle_ttl_only_reclaims_expired_runtime_and_preserves_data(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        clock = FakeClock()
        config = ServerSettings(
            data_dir=tmp_path / "data",
            sandbox_idle_ttl_seconds=7200,
        )
        workspace = WorkspaceService(config)
        launcher = FakeLauncher()
        start_watcher = AsyncMock()
        stop_watcher = AsyncMock()
        manager = RuntimeManager(
            config,
            workspace,
            EventHub(),
            launcher=launcher,
            clock=clock,
            runtime_start_hook=start_watcher,
            runtime_stop_hook=stop_watcher,
        )
        await manager.send_message("session", "task")
        root, state_root = workspace.ensure_session_directories("session")
        (root / "keep.txt").write_text("keep", encoding="utf-8")
        (state_root / "keep.txt").write_text("keep", encoding="utf-8")
        await launcher.process.stdout.feed(b"done\nyou> ")
        await wait_for_status(manager, "session", "idle")

        clock.advance(7199)
        assert await manager.sweep_expired() == ()
        assert manager.status("session") == "idle"
        clock.advance(1)
        assert await manager.sweep_expired() == ("session",)
        assert manager.status("session") == "stopped"
        stop_watcher.assert_awaited_with("session")
        assert (root / "keep.txt").exists()
        assert (state_root / "keep.txt").exists()

        assert await manager.activate("session") == "starting"
        await wait_for_status(manager, "session", "idle")
        assert start_watcher.await_count == 2
        await manager.shutdown()

    asyncio.run(scenario())


def test_connected_terminal_lease_protects_idle_runtime_from_ttl(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        clock = FakeClock()
        config = ServerSettings(
            data_dir=tmp_path / "data",
            sandbox_idle_ttl_seconds=10,
        )
        launcher = FakeLauncher()
        manager = RuntimeManager(
            config,
            WorkspaceService(config),
            EventHub(),
            launcher=launcher,
            clock=clock,
        )
        await manager.send_message("session", "task")
        await launcher.process.stdout.feed(b"done\nyou> ")
        await wait_for_status(manager, "session", "idle")

        await manager.acquire_terminal_lease("session")
        clock.advance(10)
        assert await manager.sweep_expired() == ()
        assert manager.status("session") == "idle"
        assert manager.terminal_clients("session") == 1

        await manager.release_terminal_lease("session")
        clock.advance(10)
        assert await manager.sweep_expired() == ("session",)
        await manager.shutdown()

    asyncio.run(scenario())


def test_connected_terminal_runtime_is_not_an_idle_eviction_victim(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        config = ServerSettings(
            data_dir=tmp_path / "data",
            sandbox_max_active=2,
        )
        launcher = FakeLauncher()
        manager = RuntimeManager(
            config, WorkspaceService(config), EventHub(), launcher=launcher
        )
        await manager.send_message("protected", "one")
        await manager.send_message("other", "two")
        await launcher.by_session["protected"][0].stdout.feed(b"done\nyou> ")
        await launcher.by_session["other"][0].stdout.feed(b"done\nyou> ")
        await wait_for_status(manager, "protected", "idle")
        await wait_for_status(manager, "other", "idle")
        await manager.acquire_terminal_lease("protected")

        assert await manager.send_message("new", "three") == "running"
        assert manager.status("protected") == "idle"
        assert manager.status("other") == "stopped"
        await manager.shutdown()

    asyncio.run(scenario())


def test_terminal_lease_blocks_queue_handoff_until_disconnect(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        config = replace(settings(tmp_path), sandbox_max_active=1)
        launcher = FakeLauncher()
        manager = RuntimeManager(
            config, WorkspaceService(config), EventHub(), launcher=launcher
        )
        await manager.send_message("a", "first")
        await launcher.by_session["a"][0].stdout.feed(b"done\nyou> ")
        await wait_for_status(manager, "a", "idle")
        await manager.acquire_terminal_lease("a")

        assert await manager.send_message("b", "second") == "queued"
        await asyncio.sleep(0)
        assert manager.status("a") == "idle"
        assert manager.status("b") == "queued"
        assert manager.queued_count == 1

        await manager.release_terminal_lease("a")
        await wait_for_status(manager, "b", "running")
        assert manager.status("a") == "stopped"
        assert manager.queued_count == 0
        await manager.shutdown()

    asyncio.run(scenario())


def test_last_terminal_disconnect_releases_multi_client_protection(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        config = replace(settings(tmp_path), sandbox_max_active=1)
        launcher = FakeLauncher()
        manager = RuntimeManager(
            config, WorkspaceService(config), EventHub(), launcher=launcher
        )
        await manager.send_message("a", "first")
        await launcher.by_session["a"][0].stdout.feed(b"done\nyou> ")
        await wait_for_status(manager, "a", "idle")
        await manager.acquire_terminal_lease("a")
        await manager.acquire_terminal_lease("a")
        assert manager.terminal_clients("a") == 2

        assert await manager.send_message("b", "second") == "queued"
        await manager.release_terminal_lease("a")
        await asyncio.sleep(0)
        assert manager.terminal_clients("a") == 1
        assert manager.status("a") == "idle"
        assert manager.status("b") == "queued"

        await manager.release_terminal_lease("a")
        await wait_for_status(manager, "b", "running")
        assert manager.terminal_clients("a") == 0
        assert manager.status("a") == "stopped"
        await manager.shutdown()

    asyncio.run(scenario())


def test_runtime_events_carry_web_turn_id_to_completion(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = settings(tmp_path)
        events = EventHub()
        launcher = FakeLauncher()
        manager = RuntimeManager(
            config, WorkspaceService(config), events, launcher=launcher
        )
        await manager.send_message("session", "task", turn_id="turn-1")
        await launcher.process.stdout.feed(b"assistant> answer\nyou> ")
        await wait_for_status(manager, "session", "idle")

        relevant = [
            event for event in events.history("session")
            if event.type in {"user_message", "agent_output", "runtime_status"}
        ]
        assert relevant[0].type == "user_message"
        assert relevant[0].data["turn_id"] == "turn-1"
        assert any(event.data.get("turn_id") == "turn-1" for event in relevant)
        assert any(
            event.type == "runtime_status"
            and event.data.get("status") == "idle"
            and event.data.get("turn_id") == "turn-1"
            for event in relevant
        )
        await manager.shutdown()

    asyncio.run(scenario())


def test_terminal_reservation_protects_starting_runtime_before_ready(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        config = replace(settings(tmp_path), sandbox_max_active=1)
        launcher = FakeLauncher()
        manager = RuntimeManager(
            config, WorkspaceService(config), EventHub(), launcher=launcher
        )

        await manager.acquire_terminal_lease("a")
        assert manager.terminal_clients("a") == 1
        assert await manager.activate("a") == "starting"
        assert await manager.send_message("b", "second") == "queued"

        await wait_for_status(manager, "a", "idle")
        assert manager.status("a") == "idle"
        assert manager.status("b") == "queued"
        assert manager.queued_count == 1

        await manager.release_terminal_lease("a")
        await wait_for_status(manager, "b", "running")
        assert manager.status("a") == "stopped"
        await manager.shutdown()

    asyncio.run(scenario())


def test_runtime_tokens_rotate_per_session_runtime_and_revoke_on_stop(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        config = settings(tmp_path)
        launcher = FakeLauncher()
        manager = RuntimeManager(
            config, WorkspaceService(config), EventHub(), launcher=launcher
        )
        await manager.send_message("a", "first")
        await manager.send_message("b", "second")
        token_a = manager.runtime_token("a")
        token_b = manager.runtime_token("b")
        assert token_a is not None and token_b is not None
        assert token_a != token_b
        assert manager.relay_tokens.lookup(token_a) is not None
        assert manager.relay_tokens.lookup(token_b) is not None

        await manager.stop_session("a")
        assert manager.runtime_token("a") is None
        assert manager.relay_tokens.lookup(token_a) is None
        assert manager.relay_tokens.lookup(token_b) is not None

        await manager.send_message("a", "restart")
        token_a_restart = manager.runtime_token("a")
        assert token_a_restart is not None
        assert token_a_restart != token_a
        assert manager.relay_tokens.lookup(token_a) is None
        assert manager.relay_tokens.lookup(token_a_restart) is not None
        await manager.shutdown()

    asyncio.run(scenario())


def test_runtime_start_failure_revokes_issued_token(tmp_path: Path) -> None:
    class FailingLauncher:
        async def launch(self, session_id: str, workspace: Path, mycode_state: Path):
            raise RuntimeError("launcher failed")

    async def scenario() -> None:
        config = settings(tmp_path)
        manager = RuntimeManager(
            config,
            WorkspaceService(config),
            EventHub(),
            launcher=FailingLauncher(),
        )
        with pytest.raises(RuntimeUnavailableError):
            await manager.send_message("session", "task")
        assert manager.runtime_token("session") is None
        assert manager.relay_tokens.active_count == 0
        await manager.shutdown()

    asyncio.run(scenario())


def test_runtime_workspace_start_failure_revokes_issued_token(tmp_path: Path) -> None:
    class FailingWorkspace:
        def ensure_session_directories(self, session_id: str):
            raise OSError("workspace setup failed")

    async def scenario() -> None:
        config = settings(tmp_path)
        manager = RuntimeManager(
            config,
            FailingWorkspace(),
            EventHub(),
            launcher=FakeLauncher(),
        )
        with pytest.raises(RuntimeUnavailableError):
            await manager.send_message("session", "task")
        assert manager.runtime_token("session") is None
        assert manager.relay_tokens.active_count == 0
        await manager.shutdown()

    asyncio.run(scenario())


def test_waiting_permission_expires_and_clears_pending_state(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        clock = FakeClock()
        config = ServerSettings(
            data_dir=tmp_path / "data",
            sandbox_idle_ttl_seconds=10,
        )
        events = EventHub()
        launcher = FakeLauncher()
        manager = RuntimeManager(
            config, WorkspaceService(config), events, launcher=launcher, clock=clock
        )
        await manager.send_message("session", "task")
        await launcher.process.stdout.feed(
            f"permission> run_command 需要确认\n{SCOPED_PERMISSION_PROMPT}".encode()
        )
        await wait_for_status(manager, "session", "waiting_permission")
        clock.advance(10)

        assert await manager.sweep_expired() == ("session",)
        assert manager.status("session") == "stopped"
        assert any(
            event.type == "permission_resolved" and event.data.get("expired")
            for event in events.history("session")
        )
        with pytest.raises(RuntimeConflictError):
            await manager.resolve_permission("session", "once")
        await manager.shutdown()

    asyncio.run(scenario())


def test_concurrent_admission_never_exceeds_active_limit(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = settings(tmp_path)
        launcher = FakeLauncher()
        manager = RuntimeManager(
            config, WorkspaceService(config), EventHub(), launcher=launcher
        )

        results = await asyncio.gather(
            *(manager.send_message(f"session-{index}", "task") for index in range(8))
        )

        assert results.count("running") == 2
        assert results.count("queued") == 6
        assert manager.active_count == 2
        assert launcher.calls == 2
        assert launcher.max_live_seen <= config.sandbox_max_active
        await manager.shutdown()
        assert manager.active_count == 0

    asyncio.run(scenario())
