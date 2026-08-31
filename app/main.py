from contextlib import asynccontextmanager
import asyncio
import logging

from fastapi import FastAPI

from app.api.routes import router
from app.config import ServerSettings
from app.db.database import WebDatabase
from app.services.container import AppServices
from app.services.events import EventHub
from app.services.lifecycle import SessionLifecycleService
from app.services.relay import LLMRelay
from app.services.runtime import RuntimeManager, SandboxLauncher
from app.services.workspace import WorkspaceService


logger = logging.getLogger(__name__)


async def _cleanup_orphan_sandboxes(launcher: SandboxLauncher) -> None:
    cleanup = getattr(launcher, "cleanup_orphans", None)
    if cleanup is None:
        return
    try:
        await cleanup()
    except Exception:
        logger.exception("Managed Sandbox startup cleanup failed; startup will continue.")


def create_app(
    settings: ServerSettings | None = None,
    *,
    launcher: SandboxLauncher | None = None,
) -> FastAPI:
    effective_settings = settings or ServerSettings()
    effective_settings.ensure_directories()
    database = WebDatabase(effective_settings.database_path)
    database.initialize()
    workspace = WorkspaceService(effective_settings)
    events = EventHub()
    runtime = RuntimeManager(
        effective_settings,
        workspace,
        events,
        launcher=launcher,
        activity_hook=database.touch_session,
    )
    lifecycle = SessionLifecycleService(
        effective_settings, database, workspace, runtime
    )
    relay = LLMRelay(effective_settings)
    app_services = AppServices(
        settings=effective_settings,
        database=database,
        workspace=workspace,
        events=events,
        runtime=runtime,
        lifecycle=lifecycle,
        relay=relay,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await _cleanup_orphan_sandboxes(runtime.launcher)
        await relay.start()
        await lifecycle.cleanup_expired_once()
        background_tasks = [
            asyncio.create_task(runtime.run_sweeper()),
            asyncio.create_task(lifecycle.run_cleanup_loop()),
        ]
        try:
            yield
        finally:
            for task in background_tasks:
                task.cancel()
            await asyncio.gather(*background_tasks, return_exceptions=True)
            await runtime.shutdown()
            await relay.aclose()

    application = FastAPI(title="MyCode Web Demo", lifespan=lifespan)
    application.state.services = app_services
    application.include_router(router)
    return application


app = create_app()
