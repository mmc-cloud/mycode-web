import asyncio
import codecs
from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Protocol

from app.config import ServerSettings
from app.services.events import EventHub
from app.services.terminal_adapter import TerminalOutputAdapter
from app.services.workspace import WorkspaceService


class RuntimeConflictError(RuntimeError):
    pass


class RuntimeUnavailableError(RuntimeError):
    pass


def _normalize_cli_message(content: str) -> str:
    """Convert one browser message into exactly one line for CLI stdin."""
    normalized = re.sub(
        r"[ \t]*(?:(?:\r\n|\r|\n)[ \t]*)+",
        " ",
        content.strip(),
    )
    if not normalized:
        raise ValueError("Message must not be empty.")
    return normalized


class ProcessStdin(Protocol):
    def write(self, data: bytes) -> None: ...
    async def drain(self) -> None: ...


class ProcessStdout(Protocol):
    async def read(self, size: int = -1) -> bytes: ...


class SandboxProcess(Protocol):
    stdin: ProcessStdin | None
    stdout: ProcessStdout | None
    returncode: int | None

    async def wait(self) -> int: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...


class SandboxLauncher(Protocol):
    async def launch(
        self, session_id: str, workspace: Path, mycode_state: Path
    ) -> SandboxProcess: ...


class DockerSandboxLauncher:
    def __init__(self, settings: ServerSettings) -> None:
        self.settings = settings

    def command(
        self, session_id: str, workspace: Path, mycode_state: Path
    ) -> list[str]:
        safe_session = re.sub(r"[^a-zA-Z0-9_.-]", "-", session_id)[:48]
        command = [
            self.settings.docker_command,
            "run",
            "--rm",
            "-i",
            "--name",
            f"mycode-web-{safe_session}",
            "--workdir",
            "/workspace",
            "--user",
            "mycode",
            "--network",
            "bridge",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "256",
            "--add-host",
            f"{self.settings.docker_host_alias}:host-gateway",
            "--mount",
            f"type=bind,source={workspace},target=/workspace",
            "--mount",
            f"type=bind,source={mycode_state},target=/home/mycode/.mycode",
            "-e",
            "HOME=/home/mycode",
            "-e",
            "PYTHONUNBUFFERED=1",
            "-e",
            f"MYCODE_API_KEY={self.settings.relay_token}",
            "-e",
            f"MYCODE_BASE_URL={self.settings.relay_base_url_for_sandbox}",
            "-e",
            f"MYCODE_MODEL={self.settings.model}",
        ]
        for name, value in self.settings.sandbox_optional_env:
            command.extend(["-e", f"{name}={value}"])
        command.extend([self.settings.sandbox_image, "mycode", "agent", "--continue"])
        return command

    async def launch(
        self, session_id: str, workspace: Path, mycode_state: Path
    ) -> SandboxProcess:
        return await asyncio.create_subprocess_exec(
            *self.command(session_id, workspace, mycode_state),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )


@dataclass
class _RuntimeSession:
    process: SandboxProcess | None = None
    reader_task: asyncio.Task[None] | None = None
    adapter: TerminalOutputAdapter = field(default_factory=TerminalOutputAdapter)
    decoder: object = field(
        default_factory=lambda: codecs.getincrementaldecoder("utf-8")(
            errors="replace"
        )
    )
    status: str = "stopped"
    busy: bool = False
    stopping: bool = False
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    operation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class RuntimeManager:
    def __init__(
        self,
        settings: ServerSettings,
        workspace_service: WorkspaceService,
        events: EventHub,
        launcher: SandboxLauncher | None = None,
    ) -> None:
        self.settings = settings
        self.workspace_service = workspace_service
        self.events = events
        self.launcher = launcher or DockerSandboxLauncher(settings)
        self._sessions: dict[str, _RuntimeSession] = {}

    def status(self, session_id: str) -> str:
        state = self._sessions.get(session_id)
        return "stopped" if state is None else state.status

    async def send_message(self, session_id: str, content: str) -> None:
        stdin_content = _normalize_cli_message(content)
        state = self._sessions.setdefault(session_id, _RuntimeSession())
        async with state.operation_lock:
            await self._ensure_started(session_id, state)
            if state.busy or state.adapter.awaiting_permission:
                raise RuntimeConflictError("The current Agent turn is still running.")
            state.busy = True
            state.ready.clear()
            state.status = "running"
            await self.events.publish(session_id, "runtime_status", status="running")
            await self._write(state, stdin_content + "\n")

    async def resolve_permission(self, session_id: str, allow: bool) -> None:
        state = self._sessions.get(session_id)
        if state is None or not state.adapter.awaiting_permission:
            raise RuntimeConflictError("There is no pending permission request.")
        async with state.operation_lock:
            if not state.adapter.awaiting_permission:
                raise RuntimeConflictError("There is no pending permission request.")
            await self._write(state, "y\n" if allow else "n\n")
            state.adapter.resolve_permission()
            await self.events.publish(
                session_id, "permission_resolved", allowed=allow
            )
            state.status = "running"
            await self.events.publish(session_id, "runtime_status", status="running")

    async def shutdown(self) -> None:
        await asyncio.gather(
            *(self._stop_session(state) for state in self._sessions.values()),
            return_exceptions=True,
        )

    async def _ensure_started(
        self, session_id: str, state: _RuntimeSession
    ) -> None:
        if state.process is not None and state.process.returncode is None:
            if state.status == "starting":
                await self._wait_until_ready(session_id, state)
            return
        workspace, mycode_state = self.workspace_service.ensure_session_directories(
            session_id
        )
        state.adapter = TerminalOutputAdapter()
        state.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        state.ready.clear()
        state.stopping = False
        state.status = "starting"
        await self.events.publish(session_id, "runtime_status", status="starting")
        try:
            state.process = await self.launcher.launch(
                session_id, workspace, mycode_state
            )
        except Exception as error:
            state.status = "error"
            await self.events.publish(
                session_id,
                "error",
                message=f"Sandbox failed to start: {type(error).__name__}: {error}",
            )
            raise RuntimeUnavailableError("Sandbox failed to start.") from error
        state.reader_task = asyncio.create_task(self._read_output(session_id, state))
        await self._wait_until_ready(session_id, state)

    async def _wait_until_ready(
        self, session_id: str, state: _RuntimeSession
    ) -> None:
        try:
            await asyncio.wait_for(state.ready.wait(), timeout=30)
        except TimeoutError as error:
            await self.events.publish(
                session_id,
                "error",
                message="Sandbox started but the MyCode prompt did not become ready.",
            )
            await self._stop_session(state)
            raise RuntimeUnavailableError("MyCode prompt did not become ready.") from error

    async def _read_output(
        self, session_id: str, state: _RuntimeSession
    ) -> None:
        process = state.process
        if process is None or process.stdout is None:
            return
        try:
            while True:
                chunk = await process.stdout.read(4096)
                if not chunk:
                    break
                text = state.decoder.decode(chunk)
                if not text:
                    continue
                await self.events.publish(
                    session_id, "agent_output", content=text
                )
                for signal in state.adapter.feed(text):
                    if signal.type == "permission_request":
                        await self.events.publish(
                            session_id, "permission_request", **signal.data
                        )
                        state.status = "waiting_permission"
                        await self.events.publish(
                            session_id,
                            "runtime_status",
                            status="waiting_permission",
                        )
                    elif signal.type == "ready":
                        state.busy = False
                        state.status = "idle"
                        state.ready.set()
                        await self.events.publish(
                            session_id, "runtime_status", status="idle"
                        )
            final_text = state.decoder.decode(b"", final=True)
            if final_text:
                await self.events.publish(
                    session_id, "agent_output", content=final_text
                )
            return_code = await process.wait()
            state.process = None
            state.busy = False
            state.ready.clear()
            state.status = "stopped" if state.stopping else "error"
            if not state.stopping:
                await self.events.publish(
                    session_id,
                    "error",
                    message=f"MyCode process exited with code {return_code}.",
                )
            await self.events.publish(
                session_id, "runtime_status", status=state.status
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            state.status = "error"
            await self.events.publish(
                session_id,
                "error",
                message=f"Runtime output reader failed: {type(error).__name__}: {error}",
            )

    async def _write(self, state: _RuntimeSession, content: str) -> None:
        process = state.process
        if process is None or process.returncode is not None or process.stdin is None:
            raise RuntimeUnavailableError("MyCode process is not running.")
        process.stdin.write(content.encode("utf-8"))
        await process.stdin.drain()

    async def _stop_session(self, state: _RuntimeSession) -> None:
        process = state.process
        if process is None or process.returncode is not None:
            return
        state.stopping = True
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()
        task = state.reader_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        state.process = None
        state.status = "stopped"
