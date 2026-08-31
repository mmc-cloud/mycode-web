import asyncio
from pathlib import Path

import pytest

from app.config import ServerSettings
from app.services.events import EventHub
from app.services.runtime import (
    DockerSandboxLauncher,
    RuntimeConflictError,
    RuntimeManager,
    _normalize_cli_message,
)
from app.services.workspace import WorkspaceService


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
        self.process = FakeProcess()
        self.calls = 0

    async def launch(self, session_id: str, workspace: Path, mycode_state: Path):
        self.calls += 1
        asyncio.get_running_loop().call_soon(
            self.process.stdout.queue.put_nowait, b"started\nyou> "
        )
        return self.process


def settings(tmp_path: Path) -> ServerSettings:
    return ServerSettings(
        data_dir=tmp_path / "data",
        relay_token="relay-token",
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
            "permission> run_command 需要确认\nreason> risky\n是否批准？[y/N] ".encode()
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await manager.resolve_permission("session", True)
        assert launcher.process.stdin.writes[-1] == b"y\n"
        assert manager.status("session") == "running"
        assert [event.type for event in events.history("session")][-2:] == [
            "permission_resolved",
            "runtime_status",
        ]
        assert events.history("session")[-1].data == {"status": "running"}
        await launcher.process.stdout.feed(
            "permission> write_file 需要确认\nreason> write\n是否批准？[y/N] ".encode()
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await manager.resolve_permission("session", False)
        assert launcher.process.stdin.writes[-1] == b"n\n"
        assert manager.status("session") == "running"
        assert [event.type for event in events.history("session")][-2:] == [
            "permission_resolved",
            "runtime_status",
        ]
        assert any(event.type == "permission_request" for event in events.history("session"))
        assert any(event.type == "permission_resolved" for event in events.history("session"))
        await manager.shutdown()

    asyncio.run(scenario())


def test_docker_command_mounts_only_session_and_hides_provider_key(tmp_path: Path) -> None:
    config = settings(tmp_path)
    command = DockerSandboxLauncher(config).command(
        "session", tmp_path / "workspace", tmp_path / "mycode_state"
    )
    rendered = " ".join(str(part) for part in command)
    assert "target=/workspace" in rendered
    assert "target=/home/mycode/.mycode" in rendered
    assert "MYCODE_API_KEY=relay-token" in rendered
    assert "MYCODE_BASE_URL=" in rendered
    assert "real-provider-secret" not in rendered
    assert "--user mycode" in rendered
    assert "--cap-drop ALL" in rendered


def test_docker_command_passes_only_configured_optional_mycode_env(
    tmp_path: Path,
) -> None:
    config = ServerSettings(
        data_dir=tmp_path / "data",
        relay_token="relay-token",
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
    rendered = " ".join(
        DockerSandboxLauncher(config).command(
            "session", tmp_path / "workspace", tmp_path / "mycode_state"
        )
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
