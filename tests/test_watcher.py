import asyncio
from pathlib import Path

from watchfiles import Change

from app.config import ServerSettings
from app.services.events import EventHub
from app.services.watcher import WATCH_IGNORE_DIRS, WorkspaceWatchManager
from app.services.workspace import WorkspaceService


class FakeWatchFactory:
    def __init__(self) -> None:
        self.queues: dict[Path, asyncio.Queue[set[tuple[Change, str]]]] = {}
        self.options: dict[Path, dict[str, object]] = {}

    def __call__(self, root: Path, **kwargs):
        self.options[Path(root)] = kwargs
        queue = self.queues.setdefault(Path(root), asyncio.Queue())

        async def stream():
            while True:
                yield await queue.get()

        return stream()


async def wait_for_workspace_event(events: EventHub, session_id: str) -> None:
    for _ in range(100):
        if any(event.type == "workspace_changed" for event in events.history(session_id)):
            return
        await asyncio.sleep(0)
    raise AssertionError("workspace_changed was not published")


def test_watcher_projects_create_modify_delete_rename_and_isolates_session(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        settings = ServerSettings(data_dir=tmp_path / "data", relay_token="token")
        workspace = WorkspaceService(settings)
        events = EventHub()
        factory = FakeWatchFactory()
        watcher = WorkspaceWatchManager(
            workspace, events, watch_factory=factory
        )
        root_a, _ = workspace.ensure_session_directories("a")
        root_b, _ = workspace.ensure_session_directories("b")
        await watcher.ensure("a")
        await watcher.ensure("a")
        await watcher.ensure("b")
        await asyncio.sleep(0)

        await factory.queues[root_a].put(
            {
                (Change.added, str(root_a / "created.txt")),
                (Change.modified, str(root_a / "changed.txt")),
                (Change.deleted, str(root_a / "deleted.txt")),
                (Change.deleted, str(root_a / "old-name.txt")),
                (Change.added, str(root_a / "new-name.txt")),
                (Change.added, str(root_b / "outside.txt")),
            }
        )
        await wait_for_workspace_event(events, "a")
        assert not events.history("b")
        payload = events.history("a")[-1].data["changes"]
        assert {item["change"] for item in payload} == {
            "added", "modified", "deleted"
        }
        assert {item["path"] for item in payload} == {
            "created.txt", "changed.txt", "deleted.txt",
            "old-name.txt", "new-name.txt",
        }
        await watcher.stop("a")
        assert "a" not in watcher._tasks
        await watcher.shutdown()
        assert not watcher._tasks

    asyncio.run(scenario())


def test_watcher_ignores_generated_directories_but_watches_project_files(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        settings = ServerSettings(data_dir=tmp_path / "data", relay_token="token")
        workspace = WorkspaceService(settings)
        events = EventHub()
        factory = FakeWatchFactory()
        watcher = WorkspaceWatchManager(workspace, events, watch_factory=factory)
        root, _ = workspace.ensure_session_directories("session")
        await watcher.ensure("session")
        await asyncio.sleep(0)

        watch_filter = factory.options[root]["watch_filter"]
        for directory in (
            ".venv",
            ".git",
            "node_modules",
            "__pycache__",
            ".pytest_cache",
            ".ruff_cache",
        ):
            assert directory in WATCH_IGNORE_DIRS
            assert not watch_filter(
                Change.modified, str(root / directory / "file.py")
            )
        assert watch_filter(Change.modified, str(root / "pyproject.toml"))
        assert watch_filter(Change.modified, str(root / "uv.lock"))
        assert watch_filter(Change.modified, str(root / "src" / "main.py"))
        assert watch_filter(Change.modified, str(root / "tests" / "test_main.py"))

        await watcher.shutdown()

    asyncio.run(scenario())
