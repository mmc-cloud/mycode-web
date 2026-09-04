import asyncio
from datetime import datetime, timedelta, timezone

from app.config import ServerSettings
from app.db.database import WebDatabase
from app.services.runtime import RuntimeManager
from app.services.events import EventHub
from app.services.watcher import WorkspaceWatchManager
from app.services.workspace import WorkspaceService


class SessionLifecycleService:
    def __init__(
        self,
        settings: ServerSettings,
        database: WebDatabase,
        workspace: WorkspaceService,
        runtime: RuntimeManager,
        watcher: WorkspaceWatchManager,
        events: EventHub,
    ) -> None:
        self.settings = settings
        self.database = database
        self.workspace = workspace
        self.runtime = runtime
        self.watcher = watcher
        self.events = events

    async def delete_session(
        self, session_id: str, *, user_id: str | None = None
    ) -> bool:
        if user_id is not None and self.database.get_session(session_id, user_id) is None:
            return False
        await self.runtime.stop_session(session_id)
        await self.watcher.stop(session_id)
        await asyncio.to_thread(self.workspace.delete_session_data, session_id)
        deleted = self.database.delete_session(session_id, user_id=user_id)
        if deleted:
            self.events.remove_session(session_id)
        return deleted

    async def cleanup_expired_once(
        self, *, now: datetime | None = None
    ) -> tuple[str, ...]:
        current = now or datetime.now(timezone.utc)
        cutoff = current - timedelta(seconds=self.settings.session_retention_seconds)
        session_ids = self.database.inactive_session_ids(cutoff.isoformat())
        removed: list[str] = []
        for session_id in session_ids:
            if await self.delete_session(session_id):
                removed.append(session_id)
        for user_id in self.database.inactive_user_ids(cutoff.isoformat()):
            if self.database.list_sessions(user_id):
                continue
            await asyncio.to_thread(self.workspace.delete_user_workspace, user_id)
        return tuple(removed)

    async def run_cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.session_cleanup_interval_seconds)
            await self.cleanup_expired_once()
