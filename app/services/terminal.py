import asyncio
import codecs
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
import os
import secrets
import struct
from typing import Protocol

from app.config import ServerSettings
from app.services.runtime import RuntimeManager


TERMINAL_BUFFER_LIMIT = 256 * 1024
TERMINAL_QUEUE_LIMIT = 128
MAX_TERMINAL_INPUT_BYTES = 64 * 1024
TERMINAL_PATH = "/usr/local/bin:/usr/bin:/bin"
TERMINAL_USER = "workspace-user"
TERMINAL_HOME = "/home/workspace-user"


class TerminalUnavailableError(RuntimeError):
    pass


class TerminalProcess(Protocol):
    returncode: int | None

    async def read(self, size: int = 4096) -> bytes: ...
    async def write(self, data: bytes) -> None: ...
    async def resize(self, cols: int, rows: int) -> None: ...
    async def wait(self) -> int: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...


class TerminalBackend(Protocol):
    async def launch(
        self, container_ref: str, cols: int, rows: int
    ) -> TerminalProcess: ...


class PtyTerminalProcess:
    def __init__(self, process: asyncio.subprocess.Process, master_fd: int) -> None:
        self.process = process
        self.master_fd = master_fd
        self.returncode: int | None = None

    async def read(self, size: int = 4096) -> bytes:
        try:
            return await asyncio.to_thread(os.read, self.master_fd, size)
        except OSError:
            return b""

    async def write(self, data: bytes) -> None:
        try:
            await asyncio.to_thread(os.write, self.master_fd, data)
        except OSError as error:
            raise TerminalUnavailableError("Terminal PTY is no longer available.") from error

    async def resize(self, cols: int, rows: int) -> None:
        await asyncio.to_thread(_set_pty_size, self.master_fd, cols, rows)

    async def wait(self) -> int:
        self.returncode = await self.process.wait()
        return self.returncode

    def terminate(self) -> None:
        if self.process.returncode is None:
            self.process.terminate()

    def kill(self) -> None:
        if self.process.returncode is None:
            self.process.kill()

    def close(self) -> None:
        try:
            os.close(self.master_fd)
        except OSError:
            pass


class DockerPtyTerminalBackend:
    """Linux PTY backend for an interactive bash inside an existing Sandbox."""

    def __init__(self, settings: ServerSettings) -> None:
        self.settings = settings

    def command(self, container_ref: str) -> list[str]:
        return [
            self.settings.docker_command,
            "exec",
            "--interactive",
            "--tty",
            "--env",
            "MYCODE_API_KEY=",
            "--env",
            "MYCODE_BASE_URL=",
            "--user",
            TERMINAL_USER,
            "--workdir",
            "/workspace",
            container_ref,
            "/usr/bin/env",
            "-i",
            f"HOME={TERMINAL_HOME}",
            f"PATH={TERMINAL_PATH}",
            "TERM=xterm-256color",
            "LANG=C.UTF-8",
            "PWD=/workspace",
            "/bin/bash",
            "-c",
            "umask 0002; exec /bin/bash --noprofile --norc -i",
        ]

    async def launch(
        self, container_ref: str, cols: int, rows: int
    ) -> TerminalProcess:
        if os.name != "posix":
            raise TerminalUnavailableError(
                "The real PTY terminal is supported on the Linux host only."
            )
        import pty

        master_fd, slave_fd = pty.openpty()
        try:
            _set_pty_size(master_fd, cols, rows)
            process = await asyncio.create_subprocess_exec(
                *self.command(container_ref),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
            )
        except Exception:
            os.close(master_fd)
            raise
        finally:
            os.close(slave_fd)
        return PtyTerminalProcess(process, master_fd)


@dataclass
class _TerminalState:
    process: TerminalProcess | None = None
    reader_task: asyncio.Task[None] | None = None
    start_task: asyncio.Task[None] | None = None
    clients: dict[str, asyncio.Queue[dict[str, object]]] = field(default_factory=dict)
    leased_clients: set[str] = field(default_factory=set)
    buffer: deque[bytes] = field(default_factory=deque)
    buffer_size: int = 0
    decoder: object = field(
        default_factory=lambda: codecs.getincrementaldecoder("utf-8")(
            errors="replace"
        )
    )


@dataclass(frozen=True)
class TerminalConnection:
    session_id: str
    client_id: str
    messages: asyncio.Queue[dict[str, object]]


class TerminalManager:
    """Owns one persistent shell per Session, never a Sandbox or capacity slot."""

    def __init__(
        self,
        runtime: RuntimeManager,
        settings: ServerSettings,
        *,
        backend: TerminalBackend | None = None,
        buffer_limit: int = TERMINAL_BUFFER_LIMIT,
        client_id_factory: Callable[[], str] = lambda: secrets.token_urlsafe(12),
    ) -> None:
        self.runtime = runtime
        self.settings = settings
        self.backend = backend or DockerPtyTerminalBackend(settings)
        self.buffer_limit = buffer_limit
        self._client_id_factory = client_id_factory
        self._sessions: dict[str, _TerminalState] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def attach(
        self, session_id: str, *, cols: int = 80, rows: int = 24
    ) -> TerminalConnection:
        cols, rows = _bounded_size(cols, rows)
        client_id = self._client_id_factory()
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(
            maxsize=TERMINAL_QUEUE_LIMIT
        )
        async with self._lock:
            if self._closed:
                raise TerminalUnavailableError("Terminal manager is shutting down.")
            state = self._sessions.setdefault(session_id, _TerminalState())
            state.clients[client_id] = queue
            lease_acquired = False
            try:
                await self.runtime.acquire_terminal_lease(session_id)
                lease_acquired = True
                state.leased_clients.add(client_id)
                process = state.process
                snapshot = _buffer_text(state)
                if _process_is_live(process):
                    if snapshot:
                        _queue_message(queue, {"type": "output", "data": snapshot})
                    _queue_message(queue, {"type": "status", "status": "ready"})
                    return TerminalConnection(session_id, client_id, queue)

                runtime_status = self.runtime.status(session_id)
                _queue_message(
                    queue,
                    {
                        "type": "status",
                        "status": _initial_terminal_status(runtime_status),
                    },
                )
                if state.start_task is None or state.start_task.done():
                    state.start_task = asyncio.create_task(
                        self._start_shell(session_id, state, cols, rows)
                    )
                return TerminalConnection(session_id, client_id, queue)
            except Exception:
                state.clients.pop(client_id, None)
                state.leased_clients.discard(client_id)
                if not state.clients and state.process is None and state.start_task is None:
                    self._sessions.pop(session_id, None)
                if lease_acquired:
                    await self.runtime.release_terminal_lease(session_id)
                raise

    async def detach(self, connection: TerminalConnection) -> None:
        start_task: asyncio.Task[None] | None = None
        leased = False
        async with self._lock:
            state = self._sessions.get(connection.session_id)
            if state is None:
                return
            state.clients.pop(connection.client_id, None)
            leased = connection.client_id in state.leased_clients
            state.leased_clients.discard(connection.client_id)
            if not state.clients and state.process is None:
                start_task = state.start_task
        if leased:
            await self.runtime.release_terminal_lease(connection.session_id)
        current_task = asyncio.current_task()
        if (
            start_task is not None
            and start_task is not current_task
            and not start_task.done()
        ):
            start_task.cancel()
            await asyncio.gather(start_task, return_exceptions=True)

    async def input(
        self, connection: TerminalConnection, data: str
    ) -> None:
        encoded = data.encode("utf-8")
        if not encoded or len(encoded) > MAX_TERMINAL_INPUT_BYTES:
            raise TerminalUnavailableError("Terminal input is empty or too large.")
        async with self._lock:
            state = self._sessions.get(connection.session_id)
            if state is None or connection.client_id not in state.clients:
                raise TerminalUnavailableError("Terminal connection is closed.")
            process = state.process
        if not _process_is_live(process):
            raise TerminalUnavailableError("Terminal shell is not ready.")
        await process.write(encoded)

    async def resize(
        self, connection: TerminalConnection, cols: int, rows: int
    ) -> None:
        cols, rows = _bounded_size(cols, rows)
        async with self._lock:
            state = self._sessions.get(connection.session_id)
            if state is None or connection.client_id not in state.clients:
                raise TerminalUnavailableError("Terminal connection is closed.")
            process = state.process
        if _process_is_live(process):
            await process.resize(cols, rows)

    async def stop_session(self, session_id: str) -> None:
        current_task = asyncio.current_task()
        async with self._lock:
            state = self._sessions.pop(session_id, None)
            if state is None:
                return
            start_task = state.start_task
            reader_task = state.reader_task
            process = state.process
            clients = tuple(state.clients.values())
            leased_clients = tuple(state.leased_clients)
            state.start_task = None
            state.reader_task = None
            state.process = None
            state.leased_clients.clear()
            for queue in clients:
                _queue_message(queue, {"type": "status", "status": "closed"})
        if start_task is not None and not start_task.done():
            start_task.cancel()
        if process is not None and _process_is_live(process):
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()
            _close_process(process)
        if reader_task is not None and not reader_task.done():
            reader_task.cancel()
            await asyncio.gather(reader_task, return_exceptions=True)
        if start_task is not None and start_task is not current_task:
            await asyncio.gather(start_task, return_exceptions=True)
        await asyncio.gather(
            *(self.runtime.release_terminal_lease(session_id) for _ in leased_clients),
            return_exceptions=True,
        )

    async def shutdown(self) -> None:
        self._closed = True
        async with self._lock:
            session_ids = tuple(self._sessions)
        await asyncio.gather(
            *(self.stop_session(session_id) for session_id in session_ids),
            return_exceptions=True,
        )

    async def _start_shell(
        self, session_id: str, state: _TerminalState, cols: int, rows: int
    ) -> None:
        process: TerminalProcess | None = None
        started = False
        try:
            runtime_status = await self.runtime.activate(session_id)
            await self._broadcast(
                session_id,
                {"type": "status", "status": _initial_terminal_status(runtime_status)},
            )
            await self.runtime.wait_until_ready(session_id)
            container_ref = self.runtime.container_ref(session_id)
            if not container_ref:
                raise TerminalUnavailableError(
                    "Runtime did not expose a Sandbox reference."
                )
            async with self._lock:
                if not state.clients:
                    return
            process = await self.backend.launch(container_ref, cols, rows)
            async with self._lock:
                if not state.clients:
                    should_stop = True
                else:
                    should_stop = False
                    state.process = process
                    state.reader_task = asyncio.create_task(
                        self._read_output(session_id, state, process)
                    )
            if should_stop:
                process.terminate()
                await process.wait()
                _close_process(process)
                return
            started = True
            await self._broadcast(session_id, {"type": "status", "status": "ready"})
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._broadcast(
                session_id,
                {"type": "status", "status": "error", "message": str(error)},
            )
            if process is not None and _process_is_live(process):
                process.terminate()
                await process.wait()
                _close_process(process)
        finally:
            if not started:
                async with self._lock:
                    leased = tuple(state.leased_clients)
                    state.leased_clients.clear()
                for _client_id in leased:
                    await self.runtime.release_terminal_lease(session_id)
            async with self._lock:
                if state.start_task is asyncio.current_task():
                    state.start_task = None

    async def _read_output(
        self, session_id: str, state: _TerminalState, process: TerminalProcess
    ) -> None:
        try:
            while True:
                chunk = await process.read(4096)
                if not chunk:
                    break
                state.buffer.append(bytes(chunk))
                state.buffer_size += len(chunk)
                overflow = state.buffer_size - self.buffer_limit
                while overflow > 0 and state.buffer:
                    oldest = state.buffer.popleft()
                    if len(oldest) <= overflow:
                        state.buffer_size -= len(oldest)
                        overflow -= len(oldest)
                    else:
                        state.buffer.appendleft(oldest[overflow:])
                        state.buffer_size -= overflow
                        overflow = 0
                text = state.decoder.decode(chunk)
                if text:
                    await self._broadcast(
                        session_id, {"type": "output", "data": text}
                    )
            tail = state.decoder.decode(b"", final=True)
            if tail:
                await self._broadcast(session_id, {"type": "output", "data": tail})
            return_code = await process.wait()
            status = "closed" if return_code == 0 else "error"
            await self._finish_process(session_id, state, process)
            await self._broadcast(
                session_id,
                {
                    "type": "status",
                    "status": status,
                    **({"message": f"Terminal exited with code {return_code}."} if status == "error" else {}),
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._finish_process(session_id, state, process)
            await self._broadcast(
                session_id,
                {"type": "status", "status": "error", "message": str(error)},
            )

    async def _finish_process(
        self, session_id: str, state: _TerminalState, process: TerminalProcess
    ) -> None:
        async with self._lock:
            if state.process is process:
                state.process = None
                state.reader_task = None
            leased_clients = tuple(state.leased_clients)
            state.leased_clients.clear()
        _close_process(process)
        await asyncio.gather(
            *(self.runtime.release_terminal_lease(session_id) for _ in leased_clients),
            return_exceptions=True,
        )

    async def _broadcast(
        self, session_id: str, message: dict[str, object]
    ) -> None:
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                return
            queues = tuple(state.clients.values())
            snapshot = _buffer_text(state)
        for queue in queues:
            if message.get("type") == "status" and message.get("status") in {
                "closed", "error"
            }:
                _clear_queue(queue)
            if message.get("type") == "output" and queue.full():
                _clear_queue(queue)
                _queue_message(queue, {"type": "output", "data": snapshot})
            _queue_message(queue, message)


def _bounded_size(cols: int, rows: int) -> tuple[int, int]:
    if not isinstance(cols, int) or not isinstance(rows, int):
        raise TerminalUnavailableError("Terminal size must be integer values.")
    return max(1, min(cols, 500)), max(1, min(rows, 300))


def _initial_terminal_status(runtime_status: str) -> str:
    if runtime_status == "queued":
        return "queued"
    if runtime_status in {"error", "stopped"}:
        return "starting"
    return "starting"


def _process_is_live(process: TerminalProcess | None) -> bool:
    return process is not None and process.returncode is None


def _buffer_text(state: _TerminalState) -> str:
    return b"".join(state.buffer).decode("utf-8", errors="replace")


def _queue_message(queue: asyncio.Queue[dict[str, object]], message: dict[str, object]) -> None:
    try:
        queue.put_nowait(message)
    except asyncio.QueueFull:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            pass


def _clear_queue(queue: asyncio.Queue[dict[str, object]]) -> None:
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            return


def _close_process(process: TerminalProcess) -> None:
    close = getattr(process, "close", None)
    if close is not None:
        close()


def _set_pty_size(fd: int, cols: int, rows: int) -> None:
    import fcntl
    import termios

    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
