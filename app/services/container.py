from dataclasses import dataclass

from app.config import ServerSettings
from app.db.database import WebDatabase
from app.services.events import EventHub
from app.services.lifecycle import SessionLifecycleService
from app.services.relay import LLMRelay
from app.services.runtime import RuntimeManager
from app.services.terminal import TerminalManager
from app.services.watcher import WorkspaceWatchManager
from app.services.workspace import WorkspaceService


@dataclass(frozen=True)
class AppServices:
    settings: ServerSettings
    database: WebDatabase
    workspace: WorkspaceService
    events: EventHub
    runtime: RuntimeManager
    terminal: TerminalManager
    watcher: WorkspaceWatchManager
    lifecycle: SessionLifecycleService
    relay: LLMRelay
