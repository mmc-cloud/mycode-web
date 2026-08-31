import asyncio
from datetime import datetime, timedelta, timezone

from app.config import ServerSettings
from app.db.database import WebDatabase
from app.services.runtime import RuntimeManager
from app.services.workspace import WorkspaceService


class SessionLifecycleService:
    def __init__(
        self,
        settings: ServerSettings,
        database: WebDatabase,
        workspace: WorkspaceService,
        runtime: RuntimeManager,
    ) -> None:
        self.settings = settings
        self.database = database
        self.workspace = workspace
        self.runtime = runtime

    async def cleanup_expired_once(
        self, *, now: datetime | None = None
    ) -> tuple[str, ...]:
        current = now or datetime.now(timezone.utc)
        cutoff = current - timedelta(seconds=self.settings.session_retention_seconds)
        session_ids = self.database.inactive_session_ids(cutoff.isoformat())
        removed: list[str] = []
        for session_id in session_ids:
            await self.runtime.stop_session(session_id)
            await asyncio.to_thread(self.workspace.delete_session_data, session_id)
            if self.database.delete_session(session_id):
                removed.append(session_id)
        return tuple(removed)

    async def run_cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.session_cleanup_interval_seconds)
            await self.cleanup_expired_once()
