import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
import logging
import re
import time
from typing import Coroutine, Protocol
from uuid import uuid4

from app.config import ServerSettings
from app.services.events import EventHub
from app.services.relay import RuntimeTokenRegistry
from app.services.jsonl_runtime import (
    JsonlProtocolError,
    JsonlRuntimeAdapter,
    PermissionDecision,
)
from app.services.workspace import WorkspaceService


logger = logging.getLogger(__name__)
MANAGED_SANDBOX_LABEL = "mycode-web.managed=true"
AGENT_USER = "mycode-agent"
class RuntimeConflictError(RuntimeError):
    pass


class RuntimeCapacityError(RuntimeError):
    pass


class RuntimeUnavailableError(RuntimeError):
    pass


def _validate_message_content(content: str) -> str:
    """Validate a non-empty browser message without changing its content."""
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Message must not be empty.")
    return content


def _turn_payload(turn_id: str | None) -> dict[str, str]:
    return {} if turn_id is None else {"turn_id": turn_id}


class ProcessStdin(Protocol):
    def write(self, data: bytes) -> None: ...
    async def drain(self) -> None: ...


class ProcessStdout(Protocol):
    async def read(self, size: int = -1) -> bytes: ...


class ProcessStderr(Protocol):
    async def read(self, size: int = -1) -> bytes: ...


class SandboxProcess(Protocol):
    stdin: ProcessStdin | None
    stdout: ProcessStdout | None
    stderr: ProcessStderr | None
    returncode: int | None

    async def wait(self) -> int: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...


class SandboxLauncher(Protocol):
    async def launch(
        self, session_id: str, workspace: Path, mycode_state: Path
    ) -> SandboxProcess: ...

    def container_ref(self, session_id: str) -> str: ...


def _safe_container_session(session_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "-", session_id)[:48]


class DockerSandboxLauncher:
    def __init__(self, settings: ServerSettings) -> None:
        self.settings = settings
        self._runtime_tokens: dict[str, str] = {}

    def container_ref(self, session_id: str) -> str:
        return f"mycode-web-{_safe_container_session(session_id)}"

    def set_runtime_token(self, session_id: str, token: str) -> None:
        self._runtime_tokens[session_id] = token

    def clear_runtime_token(self, session_id: str, token: str | None = None) -> None:
        if token is None or self._runtime_tokens.get(session_id) == token:
            self._runtime_tokens.pop(session_id, None)

    def command(
        self, session_id: str, workspace: Path, mycode_state: Path
    ) -> list[str]:
        runtime_token = self._runtime_tokens.get(session_id)
        if runtime_token is None:
            raise RuntimeUnavailableError(
                "Runtime relay credential has not been issued."
            )
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
            self.container_ref(session_id),
            "--workdir",
            "/workspace",
            "--user",
            AGENT_USER,
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
            f"MYCODE_API_KEY={runtime_token}",
            "-e",
            f"MYCODE_BASE_URL={self.settings.relay_base_url_for_sandbox}",
            "-e",
            f"MYCODE_MODEL={self.settings.model}",
        ]
        for name, value in self.settings.sandbox_optional_env:
            command.extend(["-e", f"{name}={value}"])
        command.extend(
            [self.settings.sandbox_image, "mycode", "runtime", "--jsonl", "--continue"]
        )
        return command

    async def launch(
        self, session_id: str, workspace: Path, mycode_state: Path
    ) -> SandboxProcess:
        return await asyncio.create_subprocess_exec(
            *self.command(session_id, workspace, mycode_state),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
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
    content: str
    turn_id: str | None


@dataclass
class _RuntimeSession:
    process: SandboxProcess | None = None
    reader_task: asyncio.Task[None] | None = None
    stderr_task: asyncio.Task[None] | None = None
    adapter: JsonlRuntimeAdapter = field(default_factory=JsonlRuntimeAdapter)
    status: str = "stopped"
    busy: bool = False
    stopping: bool = False
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    status_changed: asyncio.Event = field(default_factory=asyncio.Event)
    last_activity: float = 0.0
    container_ref: str | None = None
    active_turn_id: str | None = None
    startup_error: str | None = None
    terminal_clients: int = 0
    runtime_generation: int = 0
    relay_token: str | None = None
    pending_permission: dict[str, object] | None = None
    pending_mcp_trust: dict[str, object] | None = None
    mcp_trust_resume_status: str | None = None


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
        runtime_start_hook: Callable[[str], Awaitable[None]] | None = None,
        runtime_stop_hook: Callable[[str], Awaitable[None]] | None = None,
        relay_tokens: RuntimeTokenRegistry | None = None,
        session_owner_resolver: Callable[[str], str | None] | None = None,
    ) -> None:
        self.settings = settings
        self.workspace_service = workspace_service
        self.events = events
        self.launcher = launcher or DockerSandboxLauncher(settings)
        self._clock = clock
        self._activity_hook = activity_hook
        self._runtime_start_hook = runtime_start_hook
        self._runtime_stop_hook = runtime_stop_hook
        self.relay_tokens = relay_tokens or RuntimeTokenRegistry()
        self._session_owner_resolver = session_owner_resolver
        self._sessions: dict[str, _RuntimeSession] = {}
        self._queue: deque[_QueuedTurn] = deque()
        self._lock = asyncio.Lock()
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    def status(self, session_id: str) -> str:
        state = self._sessions.get(session_id)
        return "stopped" if state is None else state.status

    def pending_permission(self, session_id: str) -> dict[str, object] | None:
        state = self._sessions.get(session_id)
        return None if state is None else _copy_projection(state.pending_permission)

    def pending_mcp_trust(self, session_id: str) -> dict[str, object] | None:
        state = self._sessions.get(session_id)
        return None if state is None else _copy_projection(state.pending_mcp_trust)

    def active_turn_id(self, session_id: str) -> str | None:
        state = self._sessions.get(session_id)
        return None if state is None else state.active_turn_id

    def container_ref(self, session_id: str) -> str | None:
        state = self._sessions.get(session_id)
        return None if state is None else state.container_ref

    def runtime_token(self, session_id: str) -> str | None:
        state = self._sessions.get(session_id)
        return None if state is None else state.relay_token

    def runtime_generation(self, session_id: str) -> int:
        state = self._sessions.get(session_id)
        return 0 if state is None else state.runtime_generation

    async def wait_until_ready(
        self, session_id: str, *, timeout: float = 30
    ) -> str:
        startup_deadline: float | None = None
        while True:
            async with self._lock:
                state = self._sessions.get(session_id)
                if state is None:
                    raise RuntimeUnavailableError("Runtime is not active.")
                status = state.status
                if status in {
                    "idle",
                    "running",
                    "waiting_permission",
                    "waiting_mcp_trust",
                } and self._is_live(state):
                    return status
                if status == "error":
                    raise RuntimeUnavailableError(
                        f"Runtime is {status} and cannot host a terminal."
                    )
                if status == "stopped" and state.startup_error is not None:
                    raise RuntimeUnavailableError(state.startup_error)
                status_changed = state.status_changed
                status_changed.clear()
            if status == "starting":
                if startup_deadline is None:
                    startup_deadline = asyncio.get_running_loop().time() + timeout
                remaining = startup_deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise RuntimeUnavailableError("Runtime did not become ready.")
                try:
                    await asyncio.wait_for(status_changed.wait(), timeout=remaining)
                except TimeoutError as error:
                    raise RuntimeUnavailableError(
                        "Runtime did not become ready."
                    ) from error
            else:
                await status_changed.wait()

    async def acquire_terminal_lease(self, session_id: str) -> None:
        async with self._lock:
            if self._closed:
                raise RuntimeUnavailableError("Runtime manager is shutting down.")
            state = self._sessions.setdefault(session_id, _RuntimeSession())
            if state.status == "stopping":
                raise RuntimeUnavailableError("Runtime is not active.")
            state.terminal_clients += 1
            self._touch(session_id, state)

    async def release_terminal_lease(self, session_id: str) -> None:
        schedule_dispatch = False
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None or state.terminal_clients == 0:
                return
            state.terminal_clients -= 1
            self._touch(session_id, state)
            schedule_dispatch = (
                state.terminal_clients == 0
                and state.status == "idle"
                and bool(self._queue)
            )
        if schedule_dispatch:
            self._spawn(self._dispatch_from_idle(session_id, state))

    def terminal_clients(self, session_id: str) -> int:
        state = self._sessions.get(session_id)
        return 0 if state is None else state.terminal_clients

    @property
    def active_count(self) -> int:
        return sum(self._is_live(state) for state in self._sessions.values())

    @property
    def queued_count(self) -> int:
        return len(self._queue)

    async def activate(self, session_id: str) -> str:
        """Best-effort warm activation without queueing an empty Agent turn."""
        victim: tuple[str, _RuntimeSession] | None = None
        start_state: _RuntimeSession | None = None
        async with self._lock:
            if self._closed:
                raise RuntimeUnavailableError("Runtime manager is shutting down.")
            state = self._sessions.setdefault(session_id, _RuntimeSession())
            if state.status == "stopped":
                state.startup_error = None
            if state.status in {
                "starting",
                "idle",
                "running",
                "waiting_permission",
                "waiting_mcp_trust",
                "queued",
            }:
                return state.status
            if state.status == "stopping":
                return "stopped"
            if self._can_start_locked(session_id):
                self._prepare_start_locked(session_id, state)
                start_state = state
            else:
                victim = self._oldest_evictable_idle_locked(session_id)
                if victim is not None:
                    victim_id, victim_state = victim
                    self._set_status_locked(victim_state, "stopping")
                    victim_state.stopping = True
                    self._prepare_start_locked(session_id, state)
                    start_state = state
                else:
                    return "stopped"
        if start_state is None:
            return self.status(session_id)
        if victim is None:
            self._spawn(self._warm_runtime(session_id, start_state))
        else:
            self._spawn(
                self._start_after_eviction(victim, session_id, start_state, None)
            )
        return "starting"

    async def send_message(
        self, session_id: str, content: str, *, turn_id: str | None = None
    ) -> str:
        turn_content = _validate_message_content(content)
        turn_id = turn_id or uuid4().hex
        victim: tuple[str, _RuntimeSession] | None = None
        start_state: _RuntimeSession | None = None
        reuse_state: _RuntimeSession | None = None
        waiting_start_state: _RuntimeSession | None = None
        queue_position = 0

        async with self._lock:
            if self._closed:
                raise RuntimeUnavailableError("Runtime manager is shutting down.")
            state = self._sessions.setdefault(session_id, _RuntimeSession())
            if state.status == "stopped":
                state.startup_error = None
            if state.status == "starting":
                if state.active_turn_id is not None:
                    raise RuntimeConflictError(
                        "This Session already has an active Agent turn."
                    )
                state.active_turn_id = turn_id
                waiting_start_state = state
            elif state.status in {
                "queued",
                "running",
                "waiting_permission",
                "waiting_mcp_trust",
                "stopping",
            } or state.busy:
                raise RuntimeConflictError(
                    "This Session already has an active or queued Agent turn."
                )
            elif self._is_live(state) and state.status == "idle":
                self._set_status_locked(state, "running")
                state.busy = True
                state.ready.clear()
                state.active_turn_id = turn_id
                self._touch(session_id, state)
                reuse_state = state
            elif waiting_start_state is None and self._can_start_locked(session_id):
                self._prepare_start_locked(session_id, state)
                state.active_turn_id = turn_id
                start_state = state
            elif waiting_start_state is None:
                victim = self._oldest_evictable_idle_locked(session_id)
                if victim is not None:
                    victim_id, victim_state = victim
                    self._set_status_locked(victim_state, "stopping")
                    victim_state.stopping = True
                    self._prepare_start_locked(session_id, state)
                    state.active_turn_id = turn_id
                    start_state = state
                if victim is None:
                    if len(self._queue) >= self.settings.sandbox_queue_max:
                        raise RuntimeCapacityError("The Sandbox queue is full.")
                    self._set_status_locked(state, "queued")
                    state.busy = False
                    state.active_turn_id = turn_id
                    self._touch(session_id, state)
                    self._queue.append(
                        _QueuedTurn(
                            session_id=session_id,
                            content=turn_content,
                            turn_id=turn_id,
                        )
                    )
                    queue_position = len(self._queue)

        if queue_position:
            message_data = {"content": content}
            if turn_id is not None:
                message_data["turn_id"] = turn_id
            await self.events.publish(session_id, "user_message", **message_data)
            await self.events.publish(
                session_id, "runtime_status", status="queued",
                queue_position=queue_position,
                **_turn_payload(turn_id),
            )
            return "queued"
        if waiting_start_state is not None:
            message_data = {"content": content}
            if turn_id is not None:
                message_data["turn_id"] = turn_id
            await self.events.publish(session_id, "user_message", **message_data)
            await self._send_when_ready(session_id, waiting_start_state, turn_content)
            return "running"
        if reuse_state is not None:
            message_data = {"content": content}
            if turn_id is not None:
                message_data["turn_id"] = turn_id
            await self.events.publish(session_id, "user_message", **message_data)
            await self.events.publish(
                session_id, "runtime_status", status="running",
                **_turn_payload(turn_id),
            )
            try:
                await self._write_bytes(
                    reuse_state,
                    reuse_state.adapter.encode_turn(turn_id, turn_content),
                )
            except Exception:
                await self.stop_session(session_id)
                raise
            return "running"
        if victim is not None:
            await self._terminate_state(*victim, reason="capacity_eviction")
        if start_state is None:
            raise RuntimeUnavailableError("Sandbox admission failed.")
        message_data = {"content": content}
        if turn_id is not None:
            message_data["turn_id"] = turn_id
        await self.events.publish(session_id, "user_message", **message_data)
        await self._start_runtime(session_id, start_state, turn_content)
        return "running"

    async def resolve_permission(
        self,
        session_id: str,
        decision: PermissionDecision,
        request_id: str,
    ) -> None:
        if decision not in {"deny", "once", "task", "session"}:
            raise ValueError("Unsupported permission decision.")
        async with self._lock:
            state = self._sessions.get(session_id)
            pending = None if state is None else state.pending_permission
            if pending is None:
                raise RuntimeConflictError("There is no pending permission request.")
            expected_request_id = pending.get("request_id")
            if request_id != expected_request_id:
                raise RuntimeConflictError("Permission request_id is stale.")
            if not isinstance(expected_request_id, str):
                raise RuntimeConflictError("Permission request is invalid.")
            permission_data = dict(pending)
            turn_id = state.active_turn_id
            state.pending_permission = None
            self._set_status_locked(state, "running")
            self._touch(session_id, state)
        try:
            await self._write_bytes(
                state, state.adapter.encode_permission_response(expected_request_id, decision)
            )
        except Exception:
            await self.stop_session(session_id)
            raise
        await self.events.publish(
            session_id,
            "permission_resolved",
            decision=decision,
            allowed=decision != "deny",
            **permission_data,
            **_turn_payload(turn_id),
        )
        async with self._lock:
            publish_running = (
                state.active_turn_id == turn_id
                and state.status == "running"
                and self._is_live(state)
            )
        if publish_running:
            await self.events.publish(
                session_id,
                "runtime_status",
                status="running",
                **_turn_payload(turn_id),
            )

    async def resolve_mcp_trust(
        self,
        session_id: str,
        approved: bool,
        request_id: str,
    ) -> None:
        async with self._lock:
            state = self._sessions.get(session_id)
            pending = None if state is None else state.pending_mcp_trust
            if pending is None:
                raise RuntimeConflictError("There is no pending MCP trust request.")
            expected_request_id = pending.get("request_id")
            if request_id != expected_request_id:
                raise RuntimeConflictError("MCP trust request_id is stale.")
            if not isinstance(expected_request_id, str):
                raise RuntimeConflictError("MCP trust request is invalid.")
            resume_status = state.mcp_trust_resume_status or "starting"
            turn_id = state.active_turn_id
            state.pending_mcp_trust = None
            state.mcp_trust_resume_status = None
            self._set_status_locked(state, resume_status)
            self._touch(session_id, state)
        try:
            await self._write_bytes(
                state,
                state.adapter.encode_mcp_trust_response(expected_request_id, approved),
            )
        except Exception:
            await self.stop_session(session_id)
            raise
        await self.events.publish(
            session_id,
            "mcp_trust_resolved",
            request_id=expected_request_id,
            approved=approved,
            **_turn_payload(turn_id),
        )
        async with self._lock:
            publish_resume = (
                state.active_turn_id == turn_id
                and state.status == resume_status
                and self._is_live(state)
            )
        if publish_resume:
            await self.events.publish(
                session_id,
                "runtime_status",
                status=resume_status,
                **_turn_payload(turn_id),
            )

    async def sweep_expired(self) -> tuple[str, ...]:
        now = self._clock()
        expired: list[
            tuple[
                str,
                _RuntimeSession,
                str,
                dict[str, object] | None,
                dict[str, object] | None,
            ]
        ] = []
        async with self._lock:
            for session_id, state in self._sessions.items():
                if (
                    state.status in {
                        "idle",
                        "waiting_permission",
                        "waiting_mcp_trust",
                    }
                    and self._is_live(state)
                    and (
                        state.status == "waiting_mcp_trust"
                        or state.terminal_clients == 0
                    )
                    and now - state.last_activity
                    >= self.settings.sandbox_idle_ttl_seconds
                ):
                    previous = state.status
                    permission_data = (
                        dict(state.pending_permission)
                        if previous == "waiting_permission"
                        and state.pending_permission is not None
                        else None
                    )
                    mcp_trust_data = (
                        dict(state.pending_mcp_trust)
                        if previous == "waiting_mcp_trust"
                        and state.pending_mcp_trust is not None
                        else None
                    )
                    state.pending_permission = None
                    state.pending_mcp_trust = None
                    state.mcp_trust_resume_status = None
                    self._set_status_locked(state, "stopping")
                    state.stopping = True
                    expired.append(
                        (
                            session_id,
                            state,
                            previous,
                            permission_data,
                            mcp_trust_data,
                        )
                    )
        for session_id, state, previous, permission_data, mcp_trust_data in expired:
            if previous == "waiting_permission" and permission_data is not None:
                await self.events.publish(
                    session_id,
                    "permission_resolved",
                    decision="deny",
                    allowed=False,
                    expired=True,
                    **permission_data,
                    **_turn_payload(state.active_turn_id),
                )
            elif previous == "waiting_mcp_trust":
                await self.events.publish(
                    session_id,
                    "mcp_trust_resolved",
                    approved=False,
                    expired=True,
                    **(mcp_trust_data or {}),
                    **_turn_payload(state.active_turn_id),
                )
            await self._terminate_state(session_id, state, reason="inactivity_ttl")
            await self.events.publish(
                session_id, "runtime_expired",
                message="Sandbox stopped after inactivity; session data was preserved.",
            )
        if expired:
            await self._schedule_waiting()
        return tuple(
            session_id
            for session_id, _state, _previous, _permission, _mcp in expired
        )

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
                completed_turn_id = state.active_turn_id
                state.active_turn_id = None
                self._set_status_locked(state, "stopped")
                queued = True
            elif self._is_live(state) or state.status == "starting":
                self._set_status_locked(state, "stopping")
                state.stopping = True
            else:
                self._set_status_locked(state, "stopped")
        if queued:
            await self.events.publish(
                session_id, "runtime_status", status="stopped",
                **_turn_payload(completed_turn_id),
            )
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
                    self._set_status_locked(state, "stopping")
                    state.stopping = True
                elif state.status == "queued":
                    self._set_status_locked(state, "stopped")
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
        self.relay_tokens.clear()

    async def _start_runtime(
        self, session_id: str, state: _RuntimeSession, turn_content: str | None
    ) -> None:
        try:
            owner_id = (
                self._session_owner_resolver(session_id)
                if self._session_owner_resolver is not None
                else None
            )
            if self._session_owner_resolver is not None and owner_id is None:
                raise RuntimeUnavailableError("Session owner no longer exists.")
            workspace, mycode_state = self.workspace_service.ensure_session_directories(
                session_id, user_id=owner_id
            )
            await self.events.publish(
                session_id, "runtime_status", status="starting",
                **_turn_payload(state.active_turn_id),
            )
            if self._runtime_start_hook is not None:
                await self._runtime_start_hook(session_id)
            set_token = getattr(self.launcher, "set_runtime_token", None)
            if set_token is not None:
                if state.relay_token is None:
                    raise RuntimeUnavailableError(
                        "Runtime relay credential was not issued."
                    )
                set_token(session_id, state.relay_token)
            process = await self.launcher.launch(session_id, workspace, mycode_state)
            ref_factory = getattr(self.launcher, "container_ref", None)
            container_ref = ref_factory(session_id) if ref_factory else None
        except Exception as error:
            async with self._lock:
                self._set_status_locked(state, "error")
                state.busy = False
                state.ready.set()
                self._revoke_runtime_token_locked(session_id, state)
            await self.events.publish(
                session_id,
                "error",
                message=f"Sandbox failed to start: {type(error).__name__}: {error}",
                **_turn_payload(state.active_turn_id),
            )
            await self.events.publish(
                session_id, "runtime_status", status="error",
                **_turn_payload(state.active_turn_id),
            )
            if self._runtime_stop_hook is not None:
                await self._runtime_stop_hook(session_id)
            await self._schedule_waiting()
            raise RuntimeUnavailableError("Sandbox failed to start.") from error
        async with self._lock:
            if self._closed or state.status != "starting":
                should_stop = True
            else:
                should_stop = False
                state.process = process
                state.container_ref = container_ref
                state.reader_task = asyncio.create_task(
                    self._read_output(session_id, state, process)
                )
                stderr = getattr(process, "stderr", None)
                if stderr is not None:
                    state.stderr_task = asyncio.create_task(
                        self._read_stderr(session_id, stderr)
                    )
                self._touch(session_id, state)
        if should_stop:
            process.terminate()
            await process.wait()
            async with self._lock:
                self._revoke_runtime_token_locked(session_id, state)
            raise RuntimeUnavailableError("Runtime manager is shutting down.")
        startup_deadline = asyncio.get_running_loop().time() + 30
        while True:
            async with self._lock:
                if state.ready.is_set():
                    break
                if state.status in {"error", "stopped", "stopping"}:
                    raise RuntimeUnavailableError(
                        "MyCode process stopped during startup."
                    )
                status_changed = state.status_changed
                status_changed.clear()
                waiting_for_trust = state.status == "waiting_mcp_trust"
            if waiting_for_trust:
                await status_changed.wait()
                continue
            remaining = startup_deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                error = TimeoutError("runtime readiness timeout")
                break
            try:
                await asyncio.wait_for(status_changed.wait(), timeout=remaining)
            except TimeoutError as error:
                break
        if not state.ready.is_set():
            async with self._lock:
                state.startup_error = "MyCode runtime did not become ready."
            await self.events.publish(
                session_id,
                "error",
                message="Sandbox started but the MyCode runtime did not become ready.",
                **_turn_payload(state.active_turn_id),
            )
            await self.stop_session(session_id)
            raise RuntimeUnavailableError("MyCode runtime did not become ready.") from error
        async with self._lock:
            if state.status != "starting" or not self._is_live(state):
                raise RuntimeUnavailableError("MyCode process stopped during startup.")
            self._set_status_locked(
                state, "idle" if turn_content is None else "running"
            )
            state.busy = turn_content is not None
            if turn_content is not None:
                state.ready.clear()
            self._touch(session_id, state)
        await self.events.publish(
            session_id, "runtime_status", status=state.status,
            **_turn_payload(state.active_turn_id),
        )
        if turn_content is None:
            self._spawn(self._dispatch_from_idle(session_id, state))
            return
        try:
            if state.active_turn_id is None:
                raise RuntimeUnavailableError("Runtime turn_id was not assigned.")
            await self._write_bytes(
                state, state.adapter.encode_turn(state.active_turn_id, turn_content)
            )
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
                self._touch(session_id, state)
                for message in state.adapter.feed(chunk):
                    await self._handle_message(session_id, state, message)
            for message in state.adapter.finish():
                await self._handle_message(session_id, state, message)
            return_code = await process.wait()
            stderr_task = state.stderr_task
            if (
                stderr_task is not None
                and stderr_task is not asyncio.current_task()
            ):
                await asyncio.gather(stderr_task, return_exceptions=True)
            async with self._lock:
                if state.process is not process:
                    return
                completed_turn_id = state.active_turn_id
                state.process = None
                state.reader_task = None
                state.stderr_task = None
                state.busy = False
                state.ready.clear()
                state.pending_permission = None
                state.pending_mcp_trust = None
                state.mcp_trust_resume_status = None
                self._revoke_runtime_token_locked(session_id, state)
                state.active_turn_id = None
                state.container_ref = None
                stopped_intentionally = state.stopping
                clean_exit = return_code == 0
                self._set_status_locked(
                    state,
                    "stopped" if stopped_intentionally or clean_exit else "error",
                )
            if not stopped_intentionally and not clean_exit:
                await self.events.publish(
                    session_id,
                    "error",
                    message=f"MyCode process exited with code {return_code}.",
                    **_turn_payload(completed_turn_id),
                )
            await self.events.publish(
                session_id, "runtime_status", status=state.status,
                **_turn_payload(completed_turn_id),
            )
            if self._runtime_stop_hook is not None:
                await self._runtime_stop_hook(session_id)
            if not stopped_intentionally:
                await self._schedule_waiting()
        except asyncio.CancelledError:
            raise
        except JsonlProtocolError as error:
            async with self._lock:
                if state.process is process:
                    state.startup_error = str(error)
                    self._set_status_locked(state, "stopping")
                    state.stopping = True
            await self.events.publish(
                session_id,
                "error",
                code=error.code,
                message=f"Runtime JSONL protocol error: {error}",
                **_turn_payload(state.active_turn_id),
            )
            await self._terminate_state(
                session_id, state, reason="protocol_error", final_status="error"
            )
            await self._schedule_waiting()
        except Exception as error:
            async with self._lock:
                if state.process is process:
                    self._set_status_locked(state, "stopping")
                    state.stopping = True
            await self.events.publish(
                session_id,
                "error",
                message=f"Runtime output reader failed: {type(error).__name__}: {error}",
                **_turn_payload(state.active_turn_id),
            )
            await self._terminate_state(
                session_id, state, reason="reader_failure", final_status="error"
            )
            await self._schedule_waiting()

    async def _read_stderr(
        self, session_id: str, stream: ProcessStderr
    ) -> None:
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            diagnostic = chunk.decode("utf-8", errors="replace")
            logger.warning(
                "Sandbox runtime stderr session=%s bytes=%d: %s",
                session_id,
                len(chunk),
                _redact_diagnostic(diagnostic),
            )

    async def _handle_message(
        self,
        session_id: str,
        state: _RuntimeSession,
        message: dict[str, object],
    ) -> None:
        message_type = message["type"]
        if message_type == "runtime_ready":
            ready_data = _without_protocol_fields(message)
            ready_data.pop("session_id", None)
            async with self._lock:
                state.ready.set()
                state.status_changed.set()
                self._touch(session_id, state)
            await self.events.publish(
                session_id,
                "runtime_ready",
                **ready_data,
                **_turn_payload(state.active_turn_id),
            )
            return
        if message_type == "mcp_status":
            await self.events.publish(
                session_id,
                "mcp_status",
                **_without_protocol_fields(message),
                **_turn_payload(state.active_turn_id),
            )
            return
        if message_type == "mcp_trust_request":
            request_id = _required_string(message, "request_id", "mcp_trust_request")
            servers = _safe_mcp_servers(message.get("servers"))
            async with self._lock:
                state.mcp_trust_resume_status = state.status
                state.pending_mcp_trust = {
                    "request_id": request_id,
                    "servers": servers,
                }
                self._set_status_locked(state, "waiting_mcp_trust")
                self._touch(session_id, state)
            await self.events.publish(
                session_id,
                "mcp_trust_request",
                request_id=request_id,
                servers=servers,
                **_turn_payload(state.active_turn_id),
            )
            await self.events.publish(
                session_id,
                "runtime_status",
                status="waiting_mcp_trust",
                **_turn_payload(state.active_turn_id),
            )
            return
        if message_type == "permission_request":
            request_id = _required_string(message, "request_id", "permission_request")
            data = _without_protocol_fields(message)
            data["request_id"] = request_id
            bounded_data = _bounded_value(data)
            if not isinstance(bounded_data, dict):
                raise JsonlProtocolError(
                    "invalid_permission_request", "Permission request projection is invalid."
                )
            async with self._lock:
                state.pending_permission = bounded_data
                self._set_status_locked(state, "waiting_permission")
                self._touch(session_id, state)
            await self.events.publish(
                session_id, "permission_request", **bounded_data,
                **_turn_payload(state.active_turn_id),
            )
            await self.events.publish(
                session_id, "runtime_status", status="waiting_permission",
                **_turn_payload(state.active_turn_id),
            )
            return
        if message_type == "agent_event":
            event = message.get("event")
            if not isinstance(event, dict):
                raise JsonlProtocolError(
                    "invalid_agent_event", "agent_event requires an event object."
                )
            turn_id = message.get("turn_id")
            projected = _project_agent_event(event)
            await self.events.publish(
                session_id,
                "agent_event",
                turn_id=turn_id if isinstance(turn_id, str) else state.active_turn_id,
                event=projected,
            )
            return
        if message_type == "turn_finished":
            turn_id = message.get("turn_id")
            completed_turn_id = turn_id if isinstance(turn_id, str) else state.active_turn_id
            await self.events.publish(
                session_id,
                "turn_finished",
                status=message.get("status"),
                stop_reason=message.get("stop_reason"),
                **_turn_payload(completed_turn_id),
            )
            async with self._lock:
                if state.active_turn_id == completed_turn_id or completed_turn_id is None:
                    state.busy = False
                    state.pending_permission = None
                    self._set_status_locked(state, "idle")
                    self._touch(session_id, state)
                    state.active_turn_id = None
            await self.events.publish(
                session_id,
                "runtime_status",
                status="idle",
                **_turn_payload(completed_turn_id),
            )
            self._spawn(self._dispatch_from_idle(session_id, state))
            return
        if message_type == "runtime_warning":
            await self.events.publish(
                session_id,
                "runtime_warning",
                **_without_protocol_fields(message),
                **_turn_payload(state.active_turn_id),
            )
            return
        if message_type == "runtime_error":
            await self.events.publish(
                session_id,
                "runtime_error",
                **_without_protocol_fields(message),
                **_turn_payload(state.active_turn_id),
            )
            return
        if message_type == "runtime_closed":
            return
        await self.events.publish(
            session_id,
            "runtime_warning",
            code="unexpected_output",
            message=f"Ignored unknown runtime message type: {message_type}",
            **_turn_payload(state.active_turn_id),
        )

    async def _dispatch_from_idle(
        self, session_id: str, state: _RuntimeSession
    ) -> None:
        async with self._lock:
            if (
                self._closed
                or state.status != "idle"
                or state.terminal_clients > 0
                or not self._queue
            ):
                return
            item = self._pop_next_eligible_locked(releasing_session_id=session_id)
            if item is None:
                return
            target = self._sessions[item.session_id]
            self._set_status_locked(state, "stopping")
            state.stopping = True
            self._prepare_start_locked(item.session_id, target)
            target.active_turn_id = item.turn_id
        await self._terminate_state(session_id, state, reason="queue_handoff")
        try:
            await self._start_runtime(item.session_id, target, item.content)
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
                item = self._pop_next_eligible_locked()
                if item is None:
                    break
                state = self._sessions[item.session_id]
                self._prepare_start_locked(item.session_id, state)
                state.active_turn_id = item.turn_id
                starts.append((item, state))
        for item, state in starts:
            self._spawn(self._start_queued_turn(item, state))

    async def _start_queued_turn(
        self, item: _QueuedTurn, state: _RuntimeSession
    ) -> None:
        try:
            await self._start_runtime(item.session_id, state, item.content)
        except RuntimeUnavailableError:
            pass

    async def _warm_runtime(
        self, session_id: str, state: _RuntimeSession
    ) -> None:
        try:
            await self._start_runtime(session_id, state, None)
        except RuntimeUnavailableError:
            pass

    async def _start_after_eviction(
        self,
        victim: tuple[str, _RuntimeSession],
        session_id: str,
        state: _RuntimeSession,
        turn_content: str | None,
    ) -> None:
        await self._terminate_state(*victim, reason="capacity_eviction")
        try:
            await self._start_runtime(session_id, state, turn_content)
        except RuntimeUnavailableError:
            pass

    async def _send_when_ready(
        self, session_id: str, state: _RuntimeSession, turn_content: str
    ) -> None:
        await self._wait_for_runtime_ready(state)
        while True:
            async with self._lock:
                if state.status == "idle" and self._is_live(state):
                    self._set_status_locked(state, "running")
                    state.busy = True
                    state.ready.clear()
                    self._touch(session_id, state)
                    break
                if state.status in {"error", "stopped", "stopping"}:
                    raise RuntimeUnavailableError(
                        "MyCode process stopped during startup."
                    )
            await asyncio.sleep(0)
        await self.events.publish(
            session_id, "runtime_status", status="running",
            **_turn_payload(state.active_turn_id),
        )
        try:
            if state.active_turn_id is None:
                raise RuntimeUnavailableError("Runtime turn_id was not assigned.")
            await self._write_bytes(
                state, state.adapter.encode_turn(state.active_turn_id, turn_content)
            )
        except Exception:
            await self.stop_session(session_id)
            raise

    async def _wait_for_runtime_ready(self, state: _RuntimeSession) -> None:
        deadline = asyncio.get_running_loop().time() + 30
        while True:
            if state.ready.is_set():
                return
            async with self._lock:
                if state.ready.is_set():
                    return
                if state.status in {"error", "stopped", "stopping"}:
                    raise RuntimeUnavailableError(
                        "MyCode process stopped during startup."
                    )
                changed = state.status_changed
                changed.clear()
                waiting_for_trust = state.status == "waiting_mcp_trust"
            if waiting_for_trust:
                await changed.wait()
                continue
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise RuntimeUnavailableError("MyCode runtime did not become ready.")
            try:
                await asyncio.wait_for(changed.wait(), timeout=remaining)
            except TimeoutError as error:
                raise RuntimeUnavailableError(
                    "MyCode runtime did not become ready."
                ) from error

    async def _write_bytes(self, state: _RuntimeSession, payload: bytes) -> None:
        process = state.process
        if process is None or process.returncode is not None or process.stdin is None:
            raise RuntimeUnavailableError("MyCode process is not running.")
        process.stdin.write(payload)
        await process.stdin.drain()

    async def _terminate_state(
        self,
        session_id: str,
        state: _RuntimeSession,
        *,
        reason: str,
        final_status: str = "stopped",
    ) -> None:
        process = state.process
        reader_task = state.reader_task
        stderr_task = state.stderr_task
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
        if (
            stderr_task is not None
            and stderr_task is not asyncio.current_task()
            and not stderr_task.done()
        ):
            stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)
        async with self._lock:
            if state.process is process:
                state.process = None
                state.reader_task = None
                state.stderr_task = None
            state.busy = False
            state.ready.clear()
            state.stopping = False
            state.terminal_clients = 0
            completed_turn_id = state.active_turn_id
            state.pending_permission = None
            state.pending_mcp_trust = None
            state.mcp_trust_resume_status = None
            self._revoke_runtime_token_locked(session_id, state)
            state.active_turn_id = None
            state.container_ref = None
            self._set_status_locked(state, final_status)
        await self.events.publish(
            session_id, "runtime_status", status=final_status, reason=reason,
            **_turn_payload(completed_turn_id),
        )
        if self._runtime_stop_hook is not None:
            await self._runtime_stop_hook(session_id)

    def _prepare_start_locked(
        self, session_id: str, state: _RuntimeSession
    ) -> None:
        self._revoke_runtime_token_locked(session_id, state)
        state.adapter = JsonlRuntimeAdapter()
        state.ready.clear()
        state.stopping = False
        self._set_status_locked(state, "starting")
        state.busy = False
        state.container_ref = None
        state.active_turn_id = None
        state.startup_error = None
        state.runtime_generation += 1
        state.relay_token = self.relay_tokens.issue(
            session_id, state.runtime_generation
        )
        state.pending_permission = None
        state.pending_mcp_trust = None
        state.mcp_trust_resume_status = None
        self._touch(session_id, state)

    def _revoke_runtime_token_locked(
        self, session_id: str, state: _RuntimeSession
    ) -> None:
        token = state.relay_token
        state.relay_token = None
        if token is None:
            return
        self.relay_tokens.revoke(token)
        clear_token = getattr(self.launcher, "clear_runtime_token", None)
        if clear_token is not None:
            clear_token(session_id, token)

    def _can_start_locked(self, session_id: str) -> bool:
        return (
            self._occupied_slots_locked() < self.settings.sandbox_max_active
            and self._user_has_capacity_locked(session_id)
        )

    @staticmethod
    def _set_status_locked(state: _RuntimeSession, status: str) -> None:
        if state.status != status:
            state.status = status
            state.status_changed.set()

    def _user_has_capacity_locked(
        self, session_id: str, *, releasing_session_id: str | None = None
    ) -> bool:
        if self._session_owner_resolver is None:
            return True
        owner_id = self._session_owner_resolver(session_id)
        if owner_id is None:
            return True
        active_for_user = sum(
            self._occupies_slot(state)
            and candidate_id != releasing_session_id
            and self._session_owner_resolver(candidate_id) == owner_id
            for candidate_id, state in self._sessions.items()
        )
        return active_for_user < self.settings.sandbox_max_active_per_user

    def _pop_next_eligible_locked(
        self, *, releasing_session_id: str | None = None
    ) -> _QueuedTurn | None:
        index = 0
        while index < len(self._queue):
            item = self._queue[index]
            state = self._sessions.get(item.session_id)
            if state is None or state.status != "queued":
                del self._queue[index]
                continue
            if self._user_has_capacity_locked(
                item.session_id, releasing_session_id=releasing_session_id
            ):
                del self._queue[index]
                return item
            index += 1
        return None

    def _occupied_slots_locked(self) -> int:
        return sum(self._occupies_slot(state) for state in self._sessions.values())

    @classmethod
    def _occupies_slot(cls, state: _RuntimeSession) -> bool:
        return state.status == "starting" or (
            cls._is_live(state) and state.status != "stopping"
        )

    def _oldest_evictable_idle_locked(
        self, target_session_id: str
    ) -> tuple[str, _RuntimeSession] | None:
        """Return the oldest idle runtime whose slot can admit this session."""
        candidates = [
            (candidate_session_id, state)
            for candidate_session_id, state in self._sessions.items()
            if (
                state.status == "idle"
                and self._is_live(state)
                and state.terminal_clients == 0
                and self._user_has_capacity_locked(
                    target_session_id,
                    releasing_session_id=candidate_session_id,
                )
            )
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
        task.add_done_callback(self._background_task_done)

    def _background_task_done(self, task: asyncio.Task[None]) -> None:
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "Runtime background task failed",
                exc_info=(type(error), error, error.__traceback__),
            )


def _without_protocol_fields(message: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in message.items()
        if key not in {"version", "type"}
    }


def _required_string(
    message: dict[str, object], key: str, message_type: str
) -> str:
    value = message.get(key)
    if not isinstance(value, str) or not value.strip():
        raise JsonlProtocolError(
            f"missing_{key}", f"{message_type} requires a non-empty {key}."
        )
    return value


def _copy_projection(value: dict[str, object] | None) -> dict[str, object] | None:
    return None if value is None else dict(value)


def _bounded_value(value: object, *, depth: int = 0) -> object:
    if depth > 4:
        return "[nested value omitted]"
    if isinstance(value, str):
        return value if len(value) <= 16_000 else value[:15_997] + "..."
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        return {
            str(key): _bounded_value(item, depth=depth + 1)
            for key, item in list(value.items())[:64]
        }
    if isinstance(value, (list, tuple)):
        items = [_bounded_value(item, depth=depth + 1) for item in value[:64]]
        if len(value) > 64:
            items.append("[additional values omitted]")
        return items
    return str(value)[:16_000]


def _project_agent_event(event: dict[str, object]) -> dict[str, object]:
    event_type = event.get("type")
    if not isinstance(event_type, str) or not event_type:
        raise JsonlProtocolError("missing_agent_event_type", "Agent event requires a type.")
    payload: dict[str, object] = {"type": event_type}
    for key in (
        "content",
        "turn_number",
        "max_turns",
        "progress",
        "model_retry",
        "stop_reason",
        "error",
        "reasoning_state",
    ):
        if key in event:
            payload[key] = _bounded_value(event[key])
    for key in ("tool_call", "tool_result"):
        if key in event:
            payload[key] = _bounded_value(event[key])
    return payload


def _safe_mcp_servers(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise JsonlProtocolError(
            "invalid_mcp_trust_request", "mcp_trust_request requires a servers list."
        )
    result: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict):
            raise JsonlProtocolError(
                "invalid_mcp_trust_request", "MCP trust server entries must be objects."
            )
        alias = item.get("alias")
        transport = item.get("transport")
        if not isinstance(alias, str) or not isinstance(transport, str):
            raise JsonlProtocolError(
                "invalid_mcp_trust_request", "MCP trust server metadata is invalid."
            )
        safe: dict[str, object] = {"alias": alias, "transport": transport}
        if transport == "stdio":
            safe["command"] = _bounded_value(item.get("command", ""))
            safe["args"] = _bounded_value(item.get("args", []))
            safe["env_keys"] = _bounded_value(item.get("env_keys", []))
        else:
            safe["url_template"] = _bounded_value(item.get("url_template", ""))
            safe["destination"] = _bounded_value(item.get("destination", ""))
            safe["header_keys"] = _bounded_value(item.get("header_keys", []))
        result.append(safe)
    return result


def _redact_diagnostic(value: str) -> str:
    value = re.sub(
        r"(?i)(authorization|api[_-]?key|token|secret|password)(\s*[:=]\s*)\S+",
        r"\1\2[redacted]",
        value,
    )
    value = re.sub(r"\b(?:sk|rk)-[A-Za-z0-9_-]{10,}\b", "[redacted]", value)
    compact = value.replace("\r", " ").replace("\n", " ")
    return compact if len(compact) <= 2_000 else compact[:1_997] + "..."
