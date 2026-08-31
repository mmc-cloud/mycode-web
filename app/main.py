from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router
from app.config import ServerSettings
from app.db.database import WebDatabase
from app.services.container import AppServices
from app.services.events import EventHub
from app.services.relay import LLMRelay
from app.services.runtime import RuntimeManager, SandboxLauncher
from app.services.workspace import WorkspaceService


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
        effective_settings, workspace, events, launcher=launcher
    )
    relay = LLMRelay(effective_settings)
    app_services = AppServices(
        settings=effective_settings,
        database=database,
        workspace=workspace,
        events=events,
        runtime=runtime,
        relay=relay,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await relay.start()
        try:
            yield
        finally:
            await runtime.shutdown()
            await relay.aclose()

    application = FastAPI(title="MyCode Web Demo", lifespan=lifespan)
    application.state.services = app_services
    application.include_router(router)
    return application


app = create_app()
