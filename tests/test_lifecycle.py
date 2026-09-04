import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.config import ServerSettings
from app.db.database import WebDatabase
from app.main import create_app
from app.services.events import EventHub
from app.services.lifecycle import SessionLifecycleService
from app.services.runtime import RuntimeManager
from app.services.watcher import WorkspaceWatchManager
from app.services.workspace import WorkspaceError, WorkspaceService


class StartupLauncher:
    def __init__(self) -> None:
        self.cleanup_calls = 0

    async def cleanup_orphans(self) -> tuple[str, ...]:
        self.cleanup_calls += 1
        return ()

    async def launch(self, session_id, workspace, mycode_state):
        raise AssertionError("Sandbox launch is not expected in lifecycle tests.")


class FailingCleanupLauncher(StartupLauncher):
    async def cleanup_orphans(self) -> tuple[str, ...]:
        self.cleanup_calls += 1
        raise RuntimeError("simulated cleanup failure")


def make_services(tmp_path: Path):
    settings = ServerSettings(
        data_dir=tmp_path / "data",
        session_retention_seconds=14 * 24 * 60 * 60,
    )
    settings.ensure_directories()
    database = WebDatabase(settings.database_path)
    database.initialize()
    workspace = WorkspaceService(settings)
    events = EventHub()
    runtime = RuntimeManager(settings, workspace, events)
    watcher = WorkspaceWatchManager(workspace, events)
    lifecycle = SessionLifecycleService(
        settings, database, workspace, runtime, watcher, events
    )
    return settings, database, workspace, runtime, lifecycle


def test_expired_session_data_and_metadata_are_removed(tmp_path: Path) -> None:
    async def scenario() -> None:
        _settings, database, workspace, runtime, lifecycle = make_services(tmp_path)
        user, _ = database.get_or_create_user(None)
        session = database.create_session(user.id)
        workspace_root, state_root = workspace.ensure_session_directories(
            session.id, user_id=user.id
        )
        (workspace_root / "project.py").write_text("pass", encoding="utf-8")
        (state_root / "session.json").write_text("{}", encoding="utf-8")
        now = datetime(2026, 8, 31, tzinfo=timezone.utc)
        database.touch_session(
            session.id, at=(now - timedelta(days=15)).isoformat()
        )

        assert await lifecycle.cleanup_expired_once(now=now) == (session.id,)
        assert not workspace.session_dir(session.id).exists()
        assert not workspace.workspace_dir(user.id).exists()
        assert database.inactive_session_ids(now.isoformat()) == ()
        await runtime.shutdown()

    asyncio.run(scenario())


def test_recent_session_is_retained(tmp_path: Path) -> None:
    async def scenario() -> None:
        _settings, database, workspace, runtime, lifecycle = make_services(tmp_path)
        user, _ = database.get_or_create_user(None)
        session = database.create_session(user.id)
        workspace.ensure_session_directories(session.id, user_id=user.id)
        now = datetime(2026, 8, 31, tzinfo=timezone.utc)
        database.touch_session(
            session.id, at=(now - timedelta(days=13)).isoformat()
        )

        assert await lifecycle.cleanup_expired_once(now=now) == ()
        assert workspace.session_dir(session.id).exists()
        assert workspace.workspace_dir(user.id).exists()
        assert database.inactive_session_ids(now.isoformat()) == (session.id,)
        await runtime.shutdown()

    asyncio.run(scenario())


def test_expired_session_does_not_remove_workspace_with_recent_sibling(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        _settings, database, workspace, runtime, lifecycle = make_services(tmp_path)
        user, _ = database.get_or_create_user(None)
        expired = database.create_session(user.id)
        recent = database.create_session(user.id)
        workspace_root, _ = workspace.ensure_session_directories(
            expired.id, user_id=user.id
        )
        workspace.ensure_session_directories(recent.id, user_id=user.id)
        (workspace_root / "shared.txt").write_text("keep", encoding="utf-8")
        now = datetime(2026, 8, 31, tzinfo=timezone.utc)
        database.touch_session(expired.id, at=(now - timedelta(days=15)).isoformat())
        database.touch_session(recent.id, at=(now - timedelta(days=1)).isoformat())

        assert await lifecycle.cleanup_expired_once(now=now) == (expired.id,)
        assert database.get_session(recent.id, user.id) is not None
        assert workspace.workspace_dir(user.id).exists()
        assert (workspace.workspace_dir(user.id) / "shared.txt").read_text(
            encoding="utf-8"
        ) == "keep"
        await runtime.shutdown()

    asyncio.run(scenario())


def test_delete_session_stops_only_its_runtime_and_removes_its_state(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        _settings, database, workspace, runtime, lifecycle = make_services(tmp_path)
        user, _ = database.get_or_create_user(None)
        session_a = database.create_session(user.id)
        session_b = database.create_session(user.id)
        root_a, state_a = workspace.ensure_session_directories(
            session_a.id, user_id=user.id
        )
        root_b, state_b = workspace.ensure_session_directories(
            session_b.id, user_id=user.id
        )
        (root_a / "a.txt").write_text("a", encoding="utf-8")
        (state_a / "session.json").write_text("a", encoding="utf-8")
        (root_b / "b.txt").write_text("b", encoding="utf-8")
        (state_b / "session.json").write_text("b", encoding="utf-8")
        runtime.stop_session = AsyncMock(wraps=runtime.stop_session)

        assert await lifecycle.delete_session(
            session_a.id, user_id="different-user"
        ) is False
        runtime.stop_session.assert_not_awaited()
        assert root_a.exists() and state_a.exists()

        assert await lifecycle.delete_session(
            session_a.id, user_id=user.id
        ) is True
        runtime.stop_session.assert_awaited_once_with(session_a.id)
        assert not workspace.session_dir(session_a.id).exists()
        assert root_a.exists()
        assert root_b.exists() and state_b.exists()
        assert database.get_session(session_b.id, user.id) == session_b
        await runtime.shutdown()

    asyncio.run(scenario())


def test_session_cleanup_rejects_boundary_escape(tmp_path: Path) -> None:
    _settings, _database, workspace, _runtime, _lifecycle = make_services(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="Invalid session identifier"):
        workspace.delete_session_data("../outside.txt")

    assert outside.read_text(encoding="utf-8") == "keep"


def test_session_cleanup_does_not_follow_directory_symlink(tmp_path: Path) -> None:
    settings, _database, workspace, _runtime, _lifecycle = make_services(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    link = settings.sessions_dir / "linked-session"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating directory symlinks is not permitted on this host.")

    workspace.delete_session_data("linked-session")

    assert marker.read_text(encoding="utf-8") == "keep"
    assert not link.exists()


def test_application_lifespan_shuts_down_runtime(tmp_path: Path) -> None:
    settings = ServerSettings(data_dir=tmp_path / "data")
    launcher = StartupLauncher()
    app = create_app(settings, launcher=launcher)
    shutdown = AsyncMock()
    app.state.services.runtime.shutdown = shutdown

    with TestClient(app) as client:
        assert client.get("/web/api/health").status_code == 200

    assert launcher.cleanup_calls == 1
    shutdown.assert_awaited_once_with()


def test_application_startup_logs_cleanup_failure_and_continues(
    tmp_path: Path, caplog
) -> None:
    settings = ServerSettings(data_dir=tmp_path / "data")
    launcher = FailingCleanupLauncher()
    app = create_app(settings, launcher=launcher)
    caplog.set_level("ERROR")

    with TestClient(app) as client:
        assert client.get("/web/api/health").status_code == 200

    assert launcher.cleanup_calls == 1
    assert "startup cleanup failed; startup will continue" in caplog.text
