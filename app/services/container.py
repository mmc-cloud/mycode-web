from dataclasses import dataclass

from app.config import ServerSettings
from app.db.database import WebDatabase
from app.services.events import EventHub
from app.services.relay import LLMRelay
from app.services.runtime import RuntimeManager
from app.services.workspace import WorkspaceService


@dataclass(frozen=True)
class AppServices:
    settings: ServerSettings
    database: WebDatabase
    workspace: WorkspaceService
    events: EventHub
    runtime: RuntimeManager
    relay: LLMRelay
