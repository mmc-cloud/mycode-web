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


@dataclass(frozen=True)
class RuntimeTerminalTarget:
    """Atomic terminal target for one live Runtime generation."""

    generation: int
    container_ref: str


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
    terminal_lease_ids: set[str] = field(default_factory=set)
    runtime_generation: int = 0
    relay_token: str | None = None
    pending_permission: dict[str, object] | None = None
    pending_mcp_trust: dict[str, object] | None = None
    mcp_trust_resume_status: str | None = None
    readiness_pause_started: float | None = None
    readiness_pause_total: float = 0.0
    finalizing_generation: int | None = None
    finalizer_task: asyncio.Task[None] | None = None
    lifecycle_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


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
        runtime_start_hook: Callable[[str, int], Awaitable[None]] | None = None,
        runtime_stop_hook: Callable[[str, int], Awaitable[None]] | None = None,
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

    async def get_terminal_target(self, session_id: str) -> RuntimeTerminalTarget:
        """Return the current live generation/container pair as one snapshot."""
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None or not self._is_live(state):
                raise RuntimeUnavailableError("Runtime is not active.")
            if state.status in {"stopping", "stopped", "error", "queued"}:
                raise RuntimeUnavailableError("Runtime is not ready for a terminal.")
            if state.status == "starting" and not state.ready.is_set():
                raise RuntimeUnavailableError("Runtime is still starting.")
            if not state.container_ref:
                raise RuntimeUnavailableError("Runtime did not expose a Sandbox reference.")
            return RuntimeTerminalTarget(
                generation=state.runtime_generation,
                container_ref=state.container_ref,
            )

    async def wait_until_ready(
        self, session_id: str, *, timeout: float = 30
    ) -> str:
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                raise RuntimeUnavailableError("Runtime is not active.")
        return await self._wait_for_ready(
            session_id, state, timeout=timeout, return_on_ready=False
        )

    async def acquire_terminal_lease(
        self, session_id: str, lease_id: str
    ) -> None:
        if not isinstance(lease_id, str) or not lease_id:
            raise ValueError("Terminal lease_id must be a non-empty string.")
        async with self._lock:
            if self._closed:
                raise RuntimeUnavailableError("Runtime manager is shutting down.")
            state = self._sessions.setdefault(session_id, _RuntimeSession())
            if state.status == "stopping" or state.finalizing_generation is not None:
                raise RuntimeUnavailableError("Runtime is not active.")
            if lease_id in state.terminal_lease_ids:
                return
            state.terminal_lease_ids.add(lease_id)
            self._touch(session_id, state)

    async def release_terminal_lease(
        self, session_id: str, lease_id: str
    ) -> None:
        schedule_dispatch = False
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                return
            if lease_id not in state.terminal_lease_ids:
                return
            state.terminal_lease_ids.remove(lease_id)
            self._touch(session_id, state)
            schedule_dispatch = (
                not self._has_terminal_demand_locked(state)
                and self._is_genuinely_idle(state)
                and bool(self._queue)
            )
        if schedule_dispatch:
            self._spawn(self._dispatch_from_idle(session_id, state))

    def terminal_clients(self, session_id: str) -> int:
        state = self._sessions.get(session_id)
        return 0 if state is None else len(state.terminal_lease_ids)

    @staticmethod
    def _has_terminal_demand_locked(state: _RuntimeSession) -> bool:
        return bool(state.terminal_lease_ids)

    @property
    def active_count(self) -> int:
        return sum(self._is_live(state) for state in self._sessions.values())

    @property
    def queued_count(self) -> int:
        return len(self._queue)

    async def activate(self, session_id: str) -> str:
        """Best-effort warm activation without queueing an empty Agent turn."""
        victim: tuple[str, _RuntimeSession, int] | None = None
        start_state: _RuntimeSession | None = None
        dead_finalizer_task: asyncio.Task[None] | None = None
        async with self._lock:
            if self._closed:
                raise RuntimeUnavailableError("Runtime manager is shutting down.")
            state = self._sessions.setdefault(session_id, _RuntimeSession())
            if self._is_finalizing(state):
                return "stopped"
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
                if (
                    state.status != "queued"
                    and state.runtime_generation > 0
                    and not self._is_live(state)
                ):
                    dead_finalizer_task = self._begin_finalization_locked(
                        session_id,
                        state,
                        state.runtime_generation,
                        reason="dead_process_detected",
                        final_status="error",
                    )
                else:
                    return state.status
            if dead_finalizer_task is not None:
                pass
            elif state.status == "stopping":
                return "stopped"
            elif self._can_start_locked(session_id):
                self._prepare_start_locked(session_id, state)
                start_state = state
            else:
                victim = self._oldest_evictable_idle_locked(session_id)
                if victim is not None:
                    victim_id, victim_state = victim
                    victim_generation = victim_state.runtime_generation
                    self._begin_finalization_locked(
                        victim_id,
                        victim_state,
                        victim_generation,
                        reason="capacity_eviction",
                    )
                    victim = (victim_id, victim_state, victim_generation)
                    self._prepare_start_locked(session_id, state)
                    start_state = state
                else:
                    return "stopped"
        if dead_finalizer_task is not None:
            await asyncio.shield(dead_finalizer_task)
            return await self.activate(session_id)
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
        victim: tuple[str, _RuntimeSession, int] | None = None
        start_state: _RuntimeSession | None = None
        reuse_state: _RuntimeSession | None = None
        waiting_start_state: _RuntimeSession | None = None
        dead_finalizer_task: asyncio.Task[None] | None = None
        queue_position = 0

        async with self._lock:
            if self._closed:
                raise RuntimeUnavailableError("Runtime manager is shutting down.")
            state = self._sessions.setdefault(session_id, _RuntimeSession())
            if self._is_finalizing(state):
                raise RuntimeConflictError(
                    "This Session runtime is finalizing and cannot accept a new turn."
                )
            if state.status == "stopped":
                state.startup_error = None
            if (
                state.active_turn_id is None
                and state.status in {
                    "idle",
                    "running",
                    "waiting_permission",
                    "waiting_mcp_trust",
                }
                and state.runtime_generation > 0
                and not self._is_live(state)
            ):
                dead_finalizer_task = self._begin_finalization_locked(
                    session_id,
                    state,
                    state.runtime_generation,
                    reason="dead_process_detected",
                    final_status="error",
                )
            elif state.active_turn_id is not None:
                raise RuntimeConflictError(
                    "This Session already has an active or queued Agent turn."
                )
            if dead_finalizer_task is not None:
                pass
            elif state.status == "starting":
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
                    victim_generation = victim_state.runtime_generation
                    self._begin_finalization_locked(
                        victim_id,
                        victim_state,
                        victim_generation,
                        reason="capacity_eviction",
                    )
                    victim = (victim_id, victim_state, victim_generation)
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

        if dead_finalizer_task is not None:
            await asyncio.shield(dead_finalizer_task)
            return await self.send_message(session_id, content, turn_id=turn_id)
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
            await self._finalize_generation(
                victim[0],
                victim[1],
                generation=victim[2],
                reason="capacity_eviction",
            )
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
            if self._is_finalizing(state):
                raise RuntimeConflictError("Runtime is finalizing or unavailable.")
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
            if self._is_finalizing(state):
                raise RuntimeConflictError("Runtime is finalizing or unavailable.")
            expected_request_id = pending.get("request_id")
            if request_id != expected_request_id:
                raise RuntimeConflictError("MCP trust request_id is stale.")
            if not isinstance(expected_request_id, str):
                raise RuntimeConflictError("MCP trust request is invalid.")
            resume_status = state.mcp_trust_resume_status or "starting"
            turn_id = state.active_turn_id
            state.pending_mcp_trust = None
            state.mcp_trust_resume_status = None
            if state.readiness_pause_started is not None:
                state.readiness_pause_total += max(
                    0.0, self._clock() - state.readiness_pause_started
                )
            state.readiness_pause_started = None
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
                int,
                asyncio.Task[None] | None,
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
                        or (
                            state.status == "waiting_permission"
                            and not self._has_terminal_demand_locked(state)
                        )
                        or (
                            state.status == "idle"
                            and not self._has_terminal_demand_locked(state)
                            and self._is_genuinely_idle(state)
                        )
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
                    generation = state.runtime_generation
                    finalizer_task = self._begin_finalization_locked(
                        session_id,
                        state,
                        generation,
                        reason="inactivity_ttl",
                    )
                    expired.append(
                        (
                            session_id,
                            state,
                            previous,
                            permission_data,
                            mcp_trust_data,
                            generation,
                            finalizer_task,
                        )
                    )
        for (
            session_id,
            state,
            previous,
            permission_data,
            mcp_trust_data,
            _generation,
            finalizer_task,
        ) in expired:
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
            if finalizer_task is not None:
                await asyncio.shield(finalizer_task)
            await self.events.publish(
                session_id, "runtime_expired",
                message="Sandbox stopped after inactivity; session data was preserved.",
            )
        if expired:
            await self._schedule_waiting()
        return tuple(
            session_id
            for session_id, _state, _previous, _permission, _mcp, _generation, _task in expired
        )

    async def run_sweeper(self) -> None:
        while True:
            await asyncio.sleep(self.settings.runtime_sweep_interval_seconds)
            await self.sweep_expired()

    async def stop_session(self, session_id: str) -> None:
        queued = False
        state_to_finalize: _RuntimeSession | None = None
        finalizer_task: asyncio.Task[None] | None = None
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
            elif (
                self._is_live(state)
                or state.status == "starting"
                or state.finalizing_generation is not None
            ):
                state_to_finalize = state
                finalizer_task = self._begin_finalization_locked(
                    session_id,
                    state,
                    state.runtime_generation,
                    reason="session_stop",
                )
            else:
                return
        if queued:
            await self.events.publish(
                session_id, "runtime_status", status="stopped",
                **_turn_payload(completed_turn_id),
            )
            return
        if state_to_finalize is not None and finalizer_task is not None:
            await asyncio.shield(finalizer_task)

    async def shutdown(self) -> None:
        async with self._lock:
            self._closed = True
            self._queue.clear()
            sessions = list(self._sessions.items())
            finalizer_tasks: list[asyncio.Task[None]] = []
            for _session_id, state in sessions:
                if (
                    self._is_live(state)
                    or state.status == "starting"
                    or state.finalizing_generation is not None
                ):
                    task = self._begin_finalization_locked(
                        _session_id,
                        state,
                        state.runtime_generation,
                        reason="shutdown",
                    )
                    if task is not None:
                        finalizer_tasks.append(task)
                elif state.status == "queued":
                    self._set_status_locked(state, "stopped")
        await asyncio.gather(
            *(asyncio.shield(task) for task in finalizer_tasks),
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
        process: SandboxProcess | None = None
        launch_generation = state.runtime_generation
        final_status: str | None = None
        failure: Exception | None = None
        failure_message: str | None = None
        warm_dispatch = False
        try:
            async with state.lifecycle_lock:
                async with self._lock:
                    if (
                        self._closed
                        or state.runtime_generation != launch_generation
                        or state.status != "starting"
                    ):
                        return
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
                    await self._runtime_start_hook(session_id, launch_generation)
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
                async with self._lock:
                    should_stop = (
                        self._closed
                        or state.runtime_generation != launch_generation
                        or state.status != "starting"
                    )
                    if not should_stop:
                        state.process = process
                        state.container_ref = container_ref
                        state.reader_task = asyncio.create_task(
                            self._read_output(
                                session_id, state, process, launch_generation
                            )
                        )
                        stderr = getattr(process, "stderr", None)
                        if stderr is not None:
                            state.stderr_task = asyncio.create_task(
                                self._read_stderr(
                                    session_id, stderr, launch_generation
                                )
                            )
                        self._touch(session_id, state)
                if should_stop:
                    await self._terminate_process(process)
                    final_status = "stopped"
                else:
                    try:
                        await self._wait_for_ready(session_id, state, timeout=30)
                    except RuntimeUnavailableError as error:
                        failure = error
                        async with self._lock:
                            stopping = state.stopping or state.status == "stopping"
                            if not stopping:
                                state.startup_error = str(error)
                        final_status = "stopped" if stopping else "error"
                        if not stopping:
                            failure_message = (
                                "Sandbox started but the MyCode runtime did not become ready."
                            )
                    else:
                        async with self._lock:
                            if state.status != "starting" or not self._is_live(state):
                                failure = RuntimeUnavailableError(
                                    "MyCode process stopped during startup."
                                )
                                final_status = "stopped" if state.stopping else "error"
                            else:
                                self._set_status_locked(
                                    state, "idle" if turn_content is None else "running"
                                )
                                state.busy = turn_content is not None
                                if turn_content is not None:
                                    state.ready.clear()
                                self._touch(session_id, state)
                                warm_dispatch = turn_content is None
                        if final_status is None:
                            await self.events.publish(
                                session_id, "runtime_status", status=state.status,
                                **_turn_payload(state.active_turn_id),
                            )
                            if turn_content is not None:
                                try:
                                    if state.active_turn_id is None:
                                        raise RuntimeUnavailableError(
                                            "Runtime turn_id was not assigned."
                                        )
                                    await self._write_bytes(
                                        state,
                                        state.adapter.encode_turn(
                                            state.active_turn_id, turn_content
                                        ),
                                    )
                                except Exception as error:
                                    failure = error
                                    final_status = "error"
                                    failure_message = "Runtime failed to accept the Agent turn."
        except asyncio.CancelledError:
            raise
        except Exception as error:
            failure = error
            async with self._lock:
                stopping = state.stopping or state.status == "stopping"
                if not stopping:
                    state.startup_error = str(error)
            final_status = "stopped" if stopping else "error"
            if not stopping:
                failure_message = (
                    f"Sandbox failed to start: {type(error).__name__}: {error}"
                )

        if final_status is not None:
            async with self._lock:
                failed_turn_id = state.active_turn_id
                stopping = self._is_finalizing(state)
                finalizer_task = self._begin_finalization_locked(
                    session_id,
                    state,
                    launch_generation,
                    reason=(
                        "startup_failure"
                        if final_status == "error"
                        else "session_stop"
                    ),
                    final_status=final_status,
                )
            if failure_message is not None and not stopping:
                await self.events.publish(
                    session_id,
                    "error",
                    message=failure_message,
                    **_turn_payload(failed_turn_id),
                )
            if finalizer_task is not None:
                await asyncio.shield(finalizer_task)
            if failure is not None:
                raise RuntimeUnavailableError(str(failure)) from failure
            raise RuntimeUnavailableError("Runtime manager is shutting down.")
        if warm_dispatch:
            self._spawn(self._dispatch_from_idle(session_id, state))

    async def _read_output(
        self,
        session_id: str,
        state: _RuntimeSession,
        process: SandboxProcess,
        reader_generation: int,
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
            return_code = await process.wait()
            finished_messages = state.adapter.finish()
            async with self._lock:
                if (
                    state.process is not process
                    or state.runtime_generation != reader_generation
                ):
                    return
                completed_turn_id = state.active_turn_id
                stopped_intentionally = self._is_finalizing(state)
                clean_exit = return_code == 0
                final_status = "stopped" if stopped_intentionally or clean_exit else "error"
                finalizer_task = self._begin_finalization_locked(
                    session_id,
                    state,
                    reader_generation,
                    reason="process_exit",
                    final_status=final_status,
                    exclude_tasks={asyncio.current_task()},
                )
            if finalizer_task is None:
                return
            for message in finished_messages:
                await self._handle_message(session_id, state, message)
            if not stopped_intentionally and not clean_exit:
                await self.events.publish(
                    session_id,
                    "error",
                    message=f"MyCode process exited with code {return_code}.",
                    **_turn_payload(completed_turn_id),
                )
            await asyncio.shield(finalizer_task)
        except asyncio.CancelledError:
            raise
        except JsonlProtocolError as error:
            async with self._lock:
                if (
                    state.process is not process
                    or state.runtime_generation != reader_generation
                ):
                    return
                state.startup_error = str(error)
                completed_turn_id = state.active_turn_id
                finalizer_task = self._begin_finalization_locked(
                    session_id,
                    state,
                    reader_generation,
                    reason="protocol_error",
                    final_status="error",
                    exclude_tasks={asyncio.current_task()},
                )
            if finalizer_task is None:
                return
            await self.events.publish(
                session_id,
                "error",
                code=error.code,
                message=f"Runtime JSONL protocol error: {error}",
                **_turn_payload(completed_turn_id),
            )
            await asyncio.shield(finalizer_task)
        except Exception as error:
            async with self._lock:
                if (
                    state.process is not process
                    or state.runtime_generation != reader_generation
                ):
                    return
                completed_turn_id = state.active_turn_id
                finalizer_task = self._begin_finalization_locked(
                    session_id,
                    state,
                    reader_generation,
                    reason="reader_failure",
                    final_status="error",
                    exclude_tasks={asyncio.current_task()},
                )
            if finalizer_task is None:
                return
            await self.events.publish(
                session_id,
                "error",
                message=f"Runtime output reader failed: {type(error).__name__}: {error}",
                **_turn_payload(completed_turn_id),
            )
            await asyncio.shield(finalizer_task)

    async def _read_stderr(
        self, session_id: str, stream: ProcessStderr, _generation: int
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
                if self._is_finalizing(state):
                    return
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
                if self._is_finalizing(state):
                    return
                state.mcp_trust_resume_status = state.status
                state.pending_mcp_trust = {
                    "request_id": request_id,
                    "servers": servers,
                }
                state.readiness_pause_started = self._clock()
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
                if self._is_finalizing(state):
                    return
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
            completed_turn_id = turn_id if isinstance(turn_id, str) else None
            finalizing = False
            should_dispatch = False
            async with self._lock:
                finalizing = self._is_finalizing(state)
                if completed_turn_id is None:
                    completed_turn_id = state.active_turn_id
                if not finalizing and (
                    state.active_turn_id == completed_turn_id
                    or completed_turn_id is None
                ):
                    state.busy = False
                    state.pending_permission = None
                    self._set_status_locked(state, "idle")
                    self._touch(session_id, state)
                    state.active_turn_id = None
                    should_dispatch = True
            await self.events.publish(
                session_id,
                "turn_finished",
                status=message.get("status"),
                stop_reason=message.get("stop_reason"),
                **_turn_payload(completed_turn_id),
            )
            if finalizing:
                return
            await self.events.publish(
                session_id,
                "runtime_status",
                status="idle",
                **_turn_payload(completed_turn_id),
            )
            if should_dispatch:
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
        generation: int | None = None
        async with self._lock:
            if (
                self._closed
                or not self._is_genuinely_idle(state)
                or self._has_terminal_demand_locked(state)
                or not self._queue
            ):
                return
            item = self._pop_next_eligible_locked(releasing_session_id=session_id)
            if item is None:
                return
            target = self._sessions[item.session_id]
            generation = state.runtime_generation
            self._begin_finalization_locked(
                session_id,
                state,
                generation,
                reason="queue_handoff",
            )
            self._prepare_start_locked(item.session_id, target)
            target.active_turn_id = item.turn_id
        if generation is not None:
            await self._finalize_generation(
                session_id,
                state,
                generation=generation,
                reason="queue_handoff",
            )
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
        victim: tuple[str, _RuntimeSession, int],
        session_id: str,
        state: _RuntimeSession,
        turn_content: str | None,
    ) -> None:
        await self._finalize_generation(
            victim[0],
            victim[1],
            generation=victim[2],
            reason="capacity_eviction",
        )
        try:
            await self._start_runtime(session_id, state, turn_content)
        except RuntimeUnavailableError:
            pass

    async def _send_when_ready(
        self, session_id: str, state: _RuntimeSession, turn_content: str
    ) -> None:
        await self._wait_for_ready(session_id, state, timeout=30)
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

    async def _wait_for_ready(
        self,
        session_id: str,
        state: _RuntimeSession,
        *,
        timeout: float,
        return_on_ready: bool = True,
    ) -> str:
        """Wait for readiness; MCP Trust interaction time does not consume budget."""
        loop = asyncio.get_running_loop()
        budget_used = 0.0
        last_checked = self._clock()
        async with self._lock:
            last_paused_total = state.readiness_pause_total
        counted_interval = False
        while True:
            async with self._lock:
                if self._sessions.get(session_id) is not state:
                    raise RuntimeUnavailableError("Runtime is no longer active.")
                status = state.status
                if (
                    self._is_live(state)
                    and status not in {"error", "stopped", "stopping", "queued"}
                    and (
                        status in {"idle", "running", "waiting_permission"}
                        or (state.ready.is_set() and return_on_ready)
                        or (state.ready.is_set() and status != "starting")
                    )
                ):
                    return status
                if status in {"error", "stopping"} or (
                    status == "stopped"
                    and (
                        state.startup_error is not None
                        or state.runtime_generation > 0
                        or state.active_turn_id is not None
                    )
                ):
                    detail = state.startup_error or "MyCode process stopped during startup."
                    raise RuntimeUnavailableError(
                        detail
                    )
                changed = state.status_changed
                changed.clear()
                waiting_for_trust = status == "waiting_mcp_trust"
                pause_started = state.readiness_pause_started
                paused_total = state.readiness_pause_total
            now = self._clock()
            if counted_interval:
                paused_delta = max(0.0, paused_total - last_paused_total)
                if pause_started is not None:
                    paused_delta += max(
                        0.0,
                        (now - pause_started)
                        - max(0.0, last_checked - pause_started),
                    )
                budget_used += max(
                    0.0, now - last_checked - paused_delta
                )
            last_checked = now
            last_paused_total = paused_total
            counted_interval = status == "starting" and not waiting_for_trust
            if waiting_for_trust:
                await changed.wait()
                continue
            if status != "starting":
                await changed.wait()
                continue
            remaining = timeout - budget_used
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

    async def _finalize_generation(
        self,
        session_id: str,
        state: _RuntimeSession,
        *,
        generation: int,
        reason: str,
        final_status: str = "stopped",
        exclude_tasks: set[asyncio.Task[None] | None] | None = None,
    ) -> None:
        current_task = asyncio.current_task()
        excluded = set(exclude_tasks or ())
        excluded.add(current_task)
        async with self._lock:
            if self._sessions.get(session_id) is not state:
                return
            task = self._begin_finalization_locked(
                session_id,
                state,
                generation,
                reason=reason,
                final_status=final_status,
                exclude_tasks=excluded,
            )
        if task is None:
            return
        await asyncio.shield(task)

    def _claim_finalization_locked(
        self, state: _RuntimeSession, expected_generation: int
    ) -> bool:
        """Atomically make one generation the only generation that may finish."""
        if state.runtime_generation != expected_generation:
            return False
        if state.finalizing_generation is not None:
            return state.finalizing_generation == expected_generation
        if (
            state.status in {"stopped", "error"}
            and state.process is None
            and state.finalizer_task is None
        ):
            return False
        state.finalizing_generation = expected_generation
        state.stopping = True
        self._set_status_locked(state, "stopping")
        return True

    def _begin_finalization_locked(
        self,
        session_id: str,
        state: _RuntimeSession,
        generation: int,
        *,
        reason: str,
        final_status: str = "stopped",
        exclude_tasks: set[asyncio.Task[None] | None] | None = None,
    ) -> asyncio.Task[None] | None:
        if not self._claim_finalization_locked(state, generation):
            return None
        if state.finalizer_task is not None:
            return state.finalizer_task
        task = asyncio.create_task(
            self._run_generation_finalizer(
                session_id,
                state,
                generation,
                reason=reason,
                final_status=final_status,
                exclude_tasks=set(exclude_tasks or ()),
            )
        )
        state.finalizer_task = task
        return task

    async def _run_generation_finalizer(
        self,
        session_id: str,
        state: _RuntimeSession,
        cleanup_generation: int,
        *,
        reason: str,
        final_status: str,
        exclude_tasks: set[asyncio.Task[None] | None],
    ) -> None:
        completed_turn_id: str | None = None
        terminal_lease_ids: tuple[str, ...] = ()
        cleanup_errors: list[Exception] = []
        process: SandboxProcess | None = None
        converged = False
        async with state.lifecycle_lock:
            async with self._lock:
                if (
                    self._sessions.get(session_id) is not state
                    or state.runtime_generation != cleanup_generation
                ):
                    return
                process = state.process
                reader_task = state.reader_task
                stderr_task = state.stderr_task
                completed_turn_id = state.active_turn_id
                terminal_lease_ids = tuple(state.terminal_lease_ids)
            try:
                cleanup_errors.extend(await self._terminate_process(process))
            except Exception as error:
                cleanup_errors.append(error)
                logger.exception(
                    "Runtime process termination failed session=%s generation=%s",
                    session_id,
                    cleanup_generation,
                )

            for task in (reader_task, stderr_task):
                if task is None or task in exclude_tasks or task.done():
                    continue
                try:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                except Exception as error:
                    cleanup_errors.append(error)
                    logger.exception(
                        "Runtime task cleanup failed session=%s generation=%s",
                        session_id,
                        cleanup_generation,
                    )

            async with self._lock:
                if (
                    self._sessions.get(session_id) is not state
                    or state.runtime_generation != cleanup_generation
                ):
                    return
                state.process = None
                state.reader_task = None
                state.stderr_task = None
                state.ready.clear()
                try:
                    self._revoke_runtime_token_locked(session_id, state)
                except Exception as error:
                    cleanup_errors.append(error)
                    logger.exception(
                        "Runtime token cleanup failed session=%s generation=%s",
                        session_id,
                        cleanup_generation,
                    )

            if self._runtime_stop_hook is not None:
                try:
                    await self._runtime_stop_hook(session_id, cleanup_generation)
                except Exception as error:
                    cleanup_errors.append(error)
                    logger.exception(
                        "Runtime generation cleanup hook failed session=%s generation=%s",
                        session_id,
                        cleanup_generation,
                    )

            for lease_id in terminal_lease_ids:
                try:
                    await self.release_terminal_lease(session_id, lease_id)
                except Exception as error:
                    cleanup_errors.append(error)
                    logger.exception(
                        "Terminal lease cleanup failed session=%s generation=%s lease=%s",
                        session_id,
                        cleanup_generation,
                        lease_id,
                    )

            async with self._lock:
                if (
                    self._sessions.get(session_id) is not state
                    or state.runtime_generation != cleanup_generation
                ):
                    return
                state.process = None
                state.reader_task = None
                state.stderr_task = None
                state.busy = False
                state.stopping = False
                state.pending_permission = None
                state.pending_mcp_trust = None
                state.mcp_trust_resume_status = None
                state.readiness_pause_started = None
                state.readiness_pause_total = 0.0
                state.active_turn_id = None
                state.container_ref = None
                state.terminal_lease_ids.clear()
                effective_status = (
                    "error" if cleanup_errors and final_status == "stopped" else final_status
                )
                self._set_status_locked(state, effective_status)
                state.finalizing_generation = None
                state.finalizer_task = None
                converged = True

            if cleanup_errors:
                await self.events.publish(
                    session_id,
                    "runtime_warning",
                    code="runtime_cleanup_failed",
                    message=(
                        "Runtime cleanup encountered one or more errors; "
                        "the generation was converged best-effort."
                    ),
                    **_turn_payload(completed_turn_id),
                )
            if converged:
                await self.events.publish(
                    session_id,
                    "runtime_status",
                    status=effective_status,
                    reason=reason,
                    **_turn_payload(completed_turn_id),
                )
        await self._schedule_waiting()

    async def _terminate_process(
        self, process: SandboxProcess | None
    ) -> list[Exception]:
        errors: list[Exception] = []
        if process is None:
            return errors
        try:
            if process.returncode is not None:
                return errors
        except Exception as error:
            errors.append(error)
        try:
            process.terminate()
        except Exception as error:
            errors.append(error)
            logger.exception("Sandbox process terminate() failed")
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
            return errors
        except TimeoutError as error:
            errors.append(error)
            logger.warning("Sandbox process did not exit after terminate(); killing it")
        except Exception as error:
            errors.append(error)
            logger.exception("Sandbox process wait() failed after terminate()")
        try:
            process.kill()
        except Exception as error:
            errors.append(error)
            logger.exception("Sandbox process kill() failed")
        try:
            await process.wait()
        except Exception as error:
            errors.append(error)
            logger.exception("Sandbox process wait() failed after kill()")
        return errors

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
        state.readiness_pause_started = None
        state.readiness_pause_total = 0.0
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
                self._is_genuinely_idle(state)
                and not self._has_terminal_demand_locked(state)
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

    @staticmethod
    def _is_finalizing(state: _RuntimeSession) -> bool:
        return (
            state.stopping
            or state.status == "stopping"
            or state.finalizing_generation is not None
        )

    @classmethod
    def _is_genuinely_idle(cls, state: _RuntimeSession) -> bool:
        return (
            state.status == "idle"
            and cls._is_live(state)
            and state.active_turn_id is None
            and not state.busy
        )

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
