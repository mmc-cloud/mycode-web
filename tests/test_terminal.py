import asyncio
from pathlib import Path

from app.config import ServerSettings
from app.services.terminal import (
    TERMINAL_HOME,
    TERMINAL_USER,
    DockerPtyTerminalBackend,
    TerminalManager,
)


class FakeTerminalProcess:
    def __init__(self) -> None:
        self.returncode = None
        self.writes: list[bytes] = []
        self.resizes: list[tuple[int, int]] = []
        self.output: asyncio.Queue[bytes] = asyncio.Queue()
        self.done = asyncio.Event()

    async def read(self, size: int = 4096) -> bytes:
        return await self.output.get()

    async def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def resize(self, cols: int, rows: int) -> None:
        self.resizes.append((cols, rows))

    async def wait(self) -> int:
        await self.done.wait()
        return self.returncode or 0

    def terminate(self) -> None:
        self.returncode = 0
        self.done.set()
        self.output.put_nowait(b"")

    def kill(self) -> None:
        self.terminate()

    async def feed(self, content: bytes) -> None:
        await self.output.put(content)


class FakeTerminalBackend:
    def __init__(self) -> None:
        self.processes: list[FakeTerminalProcess] = []
        self.refs: list[str] = []

    async def launch(self, container_ref: str, cols: int, rows: int):
        self.refs.append(container_ref)
        process = FakeTerminalProcess()
        process.resizes.append((cols, rows))
        self.processes.append(process)
        return process


class FailingTerminalBackend:
    async def launch(self, container_ref: str, cols: int, rows: int):
        raise RuntimeError("pty backend failed")


class FakeRuntime:
    def __init__(self) -> None:
        self.states = {"session": "stopped"}
        self.refs = {"session": "mycode-web-session"}
        self.activations = 0
        self.leases = 0

    def status(self, session_id: str) -> str:
        return self.states.get(session_id, "stopped")

    async def activate(self, session_id: str) -> str:
        self.activations += 1
        if self.status(session_id) == "stopped":
            self.states[session_id] = "idle"
        return self.states[session_id]

    async def wait_until_ready(self, session_id: str) -> str:
        return self.states[session_id]

    def container_ref(self, session_id: str) -> str | None:
        return self.refs.get(session_id)

    async def acquire_terminal_lease(self, session_id: str) -> None:
        self.leases += 1

    async def release_terminal_lease(self, session_id: str) -> None:
        self.leases = max(0, self.leases - 1)


async def wait_for_status(connection, status: str) -> None:
    for _ in range(100):
        message = await asyncio.wait_for(connection.messages.get(), timeout=1)
        if message.get("type") == "status" and message.get("status") == status:
            return
    raise AssertionError(f"Terminal did not reach {status!r}.")


def test_terminal_command_uses_system_path_without_private_runtime_env(
    tmp_path: Path,
) -> None:
    settings = ServerSettings(data_dir=tmp_path / "data", relay_token="token")
    command = DockerPtyTerminalBackend(settings).command("mycode-web-session")
    rendered = " ".join(command)

    assert "PATH=/usr/local/bin:/usr/bin:/bin" in command
    assert "MYCODE_API_KEY=" in command
    assert "MYCODE_BASE_URL=" in command
    assert f"--user {TERMINAL_USER}" in rendered
    assert f"HOME={TERMINAL_HOME}" in rendered
    assert "umask 0002" in rendered
    assert "/opt/mycode-venv/bin" not in rendered
    assert not any(
        part.startswith(("UV_PROJECT_ENVIRONMENT=", "VIRTUAL_ENV=", "PYTHONPATH="))
        for part in command
    )


def test_terminal_start_failure_releases_attach_reservation(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime = FakeRuntime()
        settings = ServerSettings(data_dir=tmp_path / "data", relay_token="token")
        manager = TerminalManager(
            runtime, settings, backend=FailingTerminalBackend()
        )

        connection = await manager.attach("session")
        await wait_for_status(connection, "error")
        assert runtime.leases == 0
        await manager.shutdown()

    asyncio.run(scenario())


def test_terminal_reuses_one_shell_and_broadcasts_output(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime = FakeRuntime()
        backend = FakeTerminalBackend()
        settings = ServerSettings(data_dir=tmp_path / "data", relay_token="token")
        manager = TerminalManager(runtime, settings, backend=backend)

        first = await manager.attach("session", cols=120, rows=32)
        await wait_for_status(first, "ready")
        assert runtime.activations == 1
        assert backend.refs == ["mycode-web-session"]
        process = backend.processes[0]

        await manager.input(first, "cd src\r")
        await manager.resize(first, 140, 40)
        assert process.writes == [b"cd src\r"]
        assert process.resizes[-1] == (140, 40)

        second = await manager.attach("session")
        await wait_for_status(second, "ready")
        assert len(backend.processes) == 1
        await process.feed(b"/workspace/src\r\n")
        first_output = await asyncio.wait_for(first.messages.get(), timeout=1)
        second_output = await asyncio.wait_for(second.messages.get(), timeout=1)
        assert first_output == second_output == {
            "type": "output", "data": "/workspace/src\r\n"
        }

        await manager.detach(first)
        assert runtime.leases == 1
        await manager.detach(second)
        assert runtime.leases == 0
        await manager.shutdown()

    asyncio.run(scenario())


def test_terminal_ring_buffer_is_bounded_and_replayed(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime = FakeRuntime()
        backend = FakeTerminalBackend()
        settings = ServerSettings(data_dir=tmp_path / "data", relay_token="token")
        manager = TerminalManager(runtime, settings, backend=backend, buffer_limit=8)

        first = await manager.attach("session")
        await wait_for_status(first, "ready")
        process = backend.processes[0]
        await process.feed(b"123456")
        await process.feed(b"7890")
        await asyncio.wait_for(first.messages.get(), timeout=1)
        await asyncio.wait_for(first.messages.get(), timeout=1)

        second = await manager.attach("session")
        snapshot = await asyncio.wait_for(second.messages.get(), timeout=1)
        assert snapshot["type"] == "output"
        assert len(snapshot["data"].encode("utf-8")) <= 8
        assert snapshot["data"] == "34567890"

        await manager.stop_session("session")
        closed = await asyncio.wait_for(first.messages.get(), timeout=1)
        assert closed == {"type": "status", "status": "closed"}
        assert runtime.leases == 0

    asyncio.run(scenario())
