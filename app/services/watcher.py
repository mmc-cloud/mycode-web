import asyncio
from collections.abc import AsyncIterator, Callable
import logging
import os
from pathlib import Path

from watchfiles import Change, awatch

from app.services.events import EventHub
from app.services.workspace import WorkspaceService


logger = logging.getLogger(__name__)
WatchFactory = Callable[..., AsyncIterator[set[tuple[Change, str]]]]


class WorkspaceWatchManager:
    def __init__(
        self,
        workspace: WorkspaceService,
        events: EventHub,
        *,
        watch_factory: WatchFactory = awatch,
    ) -> None:
        self.workspace = workspace
        self.events = events
        self._watch_factory = watch_factory
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stop_events: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()

    async def ensure(self, session_id: str) -> None:
        async with self._lock:
            existing = self._tasks.get(session_id)
            if existing is not None and not existing.done():
                return
            workspace_root, _state_root = self.workspace.ensure_session_directories(
                session_id
            )
            stop_event = asyncio.Event()
            task = asyncio.create_task(
                self._watch(session_id, workspace_root, stop_event)
            )
            task.add_done_callback(
                lambda completed, session_id=session_id: self._discard_finished(
                    session_id, completed
                )
            )
            self._stop_events[session_id] = stop_event
            self._tasks[session_id] = task

    async def stop(self, session_id: str) -> None:
        async with self._lock:
            stop_event = self._stop_events.pop(session_id, None)
            task = self._tasks.pop(session_id, None)
            if stop_event is not None:
                stop_event.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def shutdown(self) -> None:
        async with self._lock:
            session_ids = tuple(self._tasks)
        await asyncio.gather(
            *(self.stop(session_id) for session_id in session_ids),
            return_exceptions=True,
        )

    async def _watch(
        self,
        session_id: str,
        workspace_root: Path,
        stop_event: asyncio.Event,
    ) -> None:
        try:
            async for changes in self._watch_factory(
                workspace_root,
                debounce=250,
                step=50,
                stop_event=stop_event,
            ):
                projected = _project_changes(workspace_root, changes)
                if projected:
                    await self.events.publish(
                        session_id,
                        "workspace_changed",
                        changes=projected,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Workspace watcher failed for Session %s", session_id)

    def _discard_finished(
        self, session_id: str, task: asyncio.Task[None]
    ) -> None:
        if self._tasks.get(session_id) is task:
            self._tasks.pop(session_id, None)
            self._stop_events.pop(session_id, None)


def _project_changes(
    workspace_root: Path, changes: set[tuple[Change, str]]
) -> list[dict[str, str]]:
    root = Path(os.path.abspath(workspace_root))
    projected: list[dict[str, str]] = []
    for change, raw_path in sorted(changes, key=lambda item: item[1]):
        candidate = Path(os.path.abspath(raw_path))
        try:
            relative = candidate.relative_to(root).as_posix()
        except ValueError:
            continue
        if not relative or relative == ".":
            continue
        projected.append({"change": change.name.lower(), "path": relative})
    return projected
