import asyncio
import codecs
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import logging
import re
import time
from typing import Coroutine, Protocol

from app.config import ServerSettings
from app.services.events import EventHub
from app.services.terminal_adapter import TerminalOutputAdapter
from app.services.workspace import WorkspaceService


logger = logging.getLogger(__name__)
MANAGED_SANDBOX_LABEL = "mycode-web.managed=true"


class RuntimeConflictError(RuntimeError):
    pass


class RuntimeCapacityError(RuntimeError):
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
            "--label",
            MANAGED_SANDBOX_LABEL,
            "--label",
            f"mycode-web.session={session_id}",
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
            "--memory",
            self.settings.sandbox_memory_limit,
            "--memory-swap",
            self.settings.sandbox_memory_swap_limit,
            "--cpus",
            str(self.settings.sandbox_cpus),
            "--pids-limit",
            str(self.settings.sandbox_pids_limit),
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

    async def cleanup_orphans(self) -> tuple[str, ...]:
        """Remove only containers selected by the exact managed label."""
        try:
            return_code, stdout, stderr = await self._run_docker(
                "ps", "-aq", "--filter", f"label={MANAGED_SANDBOX_LABEL}"
            )
        except OSError as error:
            logger.warning("Managed Sandbox cleanup could not list containers: %s", error)
            return ()
        if return_code != 0:
            logger.warning(
                "Managed Sandbox cleanup could not list containers: %s",
                stderr.strip() or f"docker exited with code {return_code}",
            )
            return ()
        container_ids = tuple(line.strip() for line in stdout.splitlines() if line.strip())
        if not container_ids:
            return ()

        try:
            remove_code, _remove_stdout, remove_stderr = await self._run_docker(
                "rm", "-f", *container_ids
            )
            if remove_code != 0:
                logger.warning(
                    "Managed Sandbox cleanup could not remove every container: %s",
                    remove_stderr.strip() or f"docker exited with code {remove_code}",
                )
        except OSError as error:
            logger.warning("Managed Sandbox cleanup could not remove containers: %s", error)
        return container_ids

    async def _run_docker(self, *arguments: str) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            self.settings.docker_command,
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return (
            process.returncode or 0,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )


@dataclass(frozen=True)
class _QueuedTurn:
    session_id: str
    original_content: str
    stdin_content: str
    enqueued_at: str


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
    last_activity: float = 0.0


class RuntimeManager:
    def __init__(
        self,
        settings: ServerSettings,
        workspace_service: WorkspaceService,
        events: EventHub,
        launcher: SandboxLauncher | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        activity_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings
        self.workspace_service = workspace_service
        self.events = events
        self.launcher = launcher or DockerSandboxLauncher(settings)
        self._clock = clock
        self._activity_hook = activity_hook
        self._sessions: dict[str, _RuntimeSession] = {}
        self._queue: deque[_QueuedTurn] = deque()
        self._lock = asyncio.Lock()
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    def status(self, session_id: str) -> str:
        state = self._sessions.get(session_id)
        return "stopped" if state is None else state.status

    @property
    def active_count(self) -> int:
        return sum(self._is_live(state) for state in self._sessions.values())

    @property
    def queued_count(self) -> int:
        return len(self._queue)

    async def send_message(self, session_id: str, content: str) -> str:
        stdin_content = _normalize_cli_message(content)
        victim: tuple[str, _RuntimeSession] | None = None
        start_state: _RuntimeSession | None = None
        reuse_state: _RuntimeSession | None = None
        queue_position = 0

        async with self._lock:
            if self._closed:
                raise RuntimeUnavailableError("Runtime manager is shutting down.")
            state = self._sessions.setdefault(session_id, _RuntimeSession())
            if state.status in {
                "queued", "starting", "running", "waiting_permission", "stopping"
            } or state.busy:
                raise RuntimeConflictError(
                    "This Session already has an active or queued Agent turn."
                )
            if self._is_live(state) and state.status == "idle":
                state.status = "running"
                state.busy = True
                state.ready.clear()
                self._touch(session_id, state)
                reuse_state = state
            elif self._occupied_slots_locked() < self.settings.sandbox_max_active:
                self._prepare_start_locked(session_id, state)
                start_state = state
            else:
                idle = self._oldest_idle_locked()
                if idle is not None:
                    victim_id, victim_state = idle
                    victim_state.status = "stopping"
                    victim_state.stopping = True
                    self._prepare_start_locked(session_id, state)
                    victim = (victim_id, victim_state)
                    start_state = state
                else:
                    if len(self._queue) >= self.settings.sandbox_queue_max:
                        raise RuntimeCapacityError("The Sandbox queue is full.")
                    state.status = "queued"
                    state.busy = False
                    self._touch(session_id, state)
                    self._queue.append(
                        _QueuedTurn(
                            session_id=session_id,
                            original_content=content,
                            stdin_content=stdin_content,
                            enqueued_at=datetime.now(timezone.utc).isoformat(),
                        )
                    )
                    queue_position = len(self._queue)

        if queue_position:
            await self.events.publish(
                session_id, "runtime_status", status="queued",
                queue_position=queue_position,
            )
            return "queued"
        if reuse_state is not None:
            await self.events.publish(session_id, "runtime_status", status="running")
            try:
                await self._write(reuse_state, stdin_content + "\n")
            except Exception:
                await self.stop_session(session_id)
                raise
            return "running"
        if victim is not None:
            await self._terminate_state(*victim, reason="capacity_eviction")
        if start_state is None:
            raise RuntimeUnavailableError("Sandbox admission failed.")
        await self._start_turn(session_id, start_state, stdin_content)
        return "running"

    async def resolve_permission(self, session_id: str, allow: bool) -> None:
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None or not state.adapter.awaiting_permission:
                raise RuntimeConflictError("There is no pending permission request.")
            state.status = "running"
            self._touch(session_id, state)
        await self._write(state, "y\n" if allow else "n\n")
        state.adapter.resolve_permission()
        await self.events.publish(session_id, "permission_resolved", allowed=allow)
        await self.events.publish(session_id, "runtime_status", status="running")

    async def sweep_expired(self) -> tuple[str, ...]:
        now = self._clock()
        expired: list[tuple[str, _RuntimeSession, str]] = []
        async with self._lock:
            for session_id, state in self._sessions.items():
                if (
                    state.status in {"idle", "waiting_permission"}
                    and self._is_live(state)
                    and now - state.last_activity
                    >= self.settings.sandbox_idle_ttl_seconds
                ):
                    previous = state.status
                    state.status = "stopping"
                    state.stopping = True
                    expired.append((session_id, state, previous))
        for session_id, state, previous in expired:
            if previous == "waiting_permission":
                state.adapter.resolve_permission()
                await self.events.publish(
                    session_id, "permission_resolved", allowed=False, expired=True
                )
            await self._terminate_state(session_id, state, reason="inactivity_ttl")
            await self.events.publish(
                session_id, "runtime_expired",
                message="Sandbox stopped after inactivity; session data was preserved.",
            )
        if expired:
            await self._schedule_waiting()
        return tuple(session_id for session_id, _state, _previous in expired)

    async def run_sweeper(self) -> None:
        while True:
            await asyncio.sleep(self.settings.runtime_sweep_interval_seconds)
            await self.sweep_expired()

    async def stop_session(self, session_id: str) -> None:
        queued = False
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                return
            if state.status == "queued":
                self._queue = deque(
                    item for item in self._queue if item.session_id != session_id
                )
                state.status = "stopped"
                queued = True
            elif self._is_live(state) or state.status == "starting":
                state.status = "stopping"
                state.stopping = True
            else:
                state.status = "stopped"
        if queued:
            await self.events.publish(session_id, "runtime_status", status="stopped")
            return
        await self._terminate_state(session_id, state, reason="session_stop")
        await self._schedule_waiting()

    async def shutdown(self) -> None:
        async with self._lock:
            self._closed = True
            self._queue.clear()
            sessions = list(self._sessions.items())
            for _session_id, state in sessions:
                if self._is_live(state) or state.status == "starting":
                    state.status = "stopping"
                    state.stopping = True
                elif state.status == "queued":
                    state.status = "stopped"
        await asyncio.gather(
            *(self._terminate_state(session_id, state, reason="shutdown")
              for session_id, state in sessions),
            return_exceptions=True,
        )
        tasks = tuple(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _start_turn(
        self, session_id: str, state: _RuntimeSession, stdin_content: str
    ) -> None:
        workspace, mycode_state = self.workspace_service.ensure_session_directories(
            session_id
        )
        await self.events.publish(session_id, "runtime_status", status="starting")
        try:
            process = await self.launcher.launch(session_id, workspace, mycode_state)
        except Exception as error:
            async with self._lock:
                state.status = "error"
                state.busy = False
                state.ready.clear()
            await self.events.publish(
                session_id,
                "error",
                message=f"Sandbox failed to start: {type(error).__name__}: {error}",
            )
            await self.events.publish(session_id, "runtime_status", status="error")
            await self._schedule_waiting()
            raise RuntimeUnavailableError("Sandbox failed to start.") from error
        async with self._lock:
            if self._closed or state.status != "starting":
                should_stop = True
            else:
                should_stop = False
                state.process = process
                state.reader_task = asyncio.create_task(
                    self._read_output(session_id, state, process)
                )
                self._touch(session_id, state)
        if should_stop:
            process.terminate()
            await process.wait()
            raise RuntimeUnavailableError("Runtime manager is shutting down.")
        try:
            await asyncio.wait_for(state.ready.wait(), timeout=30)
        except TimeoutError as error:
            await self.events.publish(
                session_id,
                "error",
                message="Sandbox started but the MyCode prompt did not become ready.",
            )
            await self.stop_session(session_id)
            raise RuntimeUnavailableError("MyCode prompt did not become ready.") from error
        async with self._lock:
            if state.status != "starting" or not self._is_live(state):
                raise RuntimeUnavailableError("MyCode process stopped during startup.")
            state.status = "running"
            state.busy = True
            state.ready.clear()
            self._touch(session_id, state)
        await self.events.publish(session_id, "runtime_status", status="running")
        try:
            await self._write(state, stdin_content + "\n")
        except Exception:
            await self.stop_session(session_id)
            raise

    async def _read_output(
        self, session_id: str, state: _RuntimeSession, process: SandboxProcess
    ) -> None:
        if process.stdout is None:
            return
        try:
            while True:
                chunk = await process.stdout.read(4096)
                if not chunk:
                    break
                text = state.decoder.decode(chunk)
                if not text:
                    continue
                self._touch(session_id, state)
                await self.events.publish(session_id, "agent_output", content=text)
                for signal in state.adapter.feed(text):
                    await self._handle_signal(
                        session_id, state, signal.type, signal.data
                    )
            final_text = state.decoder.decode(b"", final=True)
            if final_text:
                self._touch(session_id, state)
                await self.events.publish(
                    session_id, "agent_output", content=final_text
                )
            return_code = await process.wait()
            async with self._lock:
                if state.process is not process:
                    return
                state.process = None
                state.reader_task = None
                state.busy = False
                state.ready.clear()
                stopped_intentionally = state.stopping
                state.status = "stopped" if stopped_intentionally else "error"
            if not stopped_intentionally:
                await self.events.publish(
                    session_id,
                    "error",
                    message=f"MyCode process exited with code {return_code}.",
                )
            await self.events.publish(
                session_id, "runtime_status", status=state.status
            )
            if not stopped_intentionally:
                await self._schedule_waiting()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            async with self._lock:
                if state.process is process:
                    state.status = "stopping"
                    state.stopping = True
            await self.events.publish(
                session_id,
                "error",
                message=f"Runtime output reader failed: {type(error).__name__}: {error}",
            )
            await self._terminate_state(
                session_id, state, reason="reader_failure"
            )
            await self._schedule_waiting()

    async def _handle_signal(
        self, session_id: str, state: _RuntimeSession,
        signal_type: str, data: dict[str, object],
    ) -> None:
        if signal_type == "permission_request":
            async with self._lock:
                if state.status != "running":
                    return
                state.status = "waiting_permission"
                self._touch(session_id, state)
            await self.events.publish(session_id, "permission_request", **data)
            await self.events.publish(
                session_id, "runtime_status", status="waiting_permission"
            )
            return
        if signal_type != "ready":
            return
        async with self._lock:
            self._touch(session_id, state)
            if state.status == "starting":
                state.ready.set()
                return
            if state.status not in {"running", "waiting_permission"}:
                return
            state.busy = False
            state.status = "idle"
            state.ready.set()
        await self.events.publish(session_id, "runtime_status", status="idle")
        self._spawn(self._dispatch_from_idle(session_id, state))

    async def _dispatch_from_idle(
        self, session_id: str, state: _RuntimeSession
    ) -> None:
        async with self._lock:
            if self._closed or state.status != "idle" or not self._queue:
                return
            item = self._queue.popleft()
            target = self._sessions[item.session_id]
            state.status = "stopping"
            state.stopping = True
            self._prepare_start_locked(item.session_id, target)
        await self._terminate_state(session_id, state, reason="queue_handoff")
        try:
            await self._start_turn(item.session_id, target, item.stdin_content)
        except RuntimeUnavailableError:
            pass

    async def _schedule_waiting(self) -> None:
        starts: list[tuple[_QueuedTurn, _RuntimeSession]] = []
        async with self._lock:
            if self._closed:
                return
            while (
                self._queue
                and self._occupied_slots_locked() < self.settings.sandbox_max_active
            ):
                item = self._queue.popleft()
                state = self._sessions[item.session_id]
                self._prepare_start_locked(item.session_id, state)
                starts.append((item, state))
        for item, state in starts:
            self._spawn(self._start_queued_turn(item, state))

    async def _start_queued_turn(
        self, item: _QueuedTurn, state: _RuntimeSession
    ) -> None:
        try:
            await self._start_turn(item.session_id, state, item.stdin_content)
        except RuntimeUnavailableError:
            pass

    async def _write(self, state: _RuntimeSession, content: str) -> None:
        process = state.process
        if process is None or process.returncode is not None or process.stdin is None:
            raise RuntimeUnavailableError("MyCode process is not running.")
        process.stdin.write(content.encode("utf-8"))
        await process.stdin.drain()

    async def _terminate_state(
        self, session_id: str, state: _RuntimeSession, *, reason: str
    ) -> None:
        process = state.process
        reader_task = state.reader_task
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()
        if (
            reader_task is not None
            and reader_task is not asyncio.current_task()
            and not reader_task.done()
        ):
            reader_task.cancel()
            await asyncio.gather(reader_task, return_exceptions=True)
        async with self._lock:
            if state.process is process:
                state.process = None
                state.reader_task = None
            state.busy = False
            state.ready.clear()
            state.stopping = False
            state.status = "stopped"
        await self.events.publish(
            session_id, "runtime_status", status="stopped", reason=reason
        )

    def _prepare_start_locked(
        self, session_id: str, state: _RuntimeSession
    ) -> None:
        state.adapter = TerminalOutputAdapter()
        state.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        state.ready.clear()
        state.stopping = False
        state.status = "starting"
        state.busy = False
        self._touch(session_id, state)

    def _occupied_slots_locked(self) -> int:
        return sum(
            state.status == "starting"
            or (self._is_live(state) and state.status != "stopping")
            for state in self._sessions.values()
        )

    def _oldest_idle_locked(self) -> tuple[str, _RuntimeSession] | None:
        candidates = [
            (session_id, state)
            for session_id, state in self._sessions.items()
            if state.status == "idle" and self._is_live(state)
        ]
        return min(candidates, key=lambda item: item[1].last_activity, default=None)

    @staticmethod
    def _is_live(state: _RuntimeSession) -> bool:
        return state.process is not None and state.process.returncode is None

    def _touch(self, session_id: str, state: _RuntimeSession) -> None:
        state.last_activity = self._clock()
        if self._activity_hook is not None:
            try:
                asyncio.get_running_loop().call_soon(
                    self._record_persistent_activity, session_id
                )
            except RuntimeError:
                pass

    def _record_persistent_activity(self, session_id: str) -> None:
        if self._activity_hook is None:
            return
        try:
            self._activity_hook(session_id)
        except Exception:
            pass

    def _spawn(self, coroutine: Coroutine[object, object, None]) -> None:
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
