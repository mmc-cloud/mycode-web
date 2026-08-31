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
        relay_token="token",
        session_retention_seconds=14 * 24 * 60 * 60,
    )
    settings.ensure_directories()
    database = WebDatabase(settings.database_path)
    database.initialize()
    workspace = WorkspaceService(settings)
    runtime = RuntimeManager(settings, workspace, EventHub())
    lifecycle = SessionLifecycleService(settings, database, workspace, runtime)
    return settings, database, workspace, runtime, lifecycle


def test_expired_session_data_and_metadata_are_removed(tmp_path: Path) -> None:
    async def scenario() -> None:
        _settings, database, workspace, runtime, lifecycle = make_services(tmp_path)
        user, _ = database.get_or_create_user(None)
        session = database.ensure_session(user.id)
        workspace_root, state_root = workspace.ensure_session_directories(session.id)
        (workspace_root / "project.py").write_text("pass", encoding="utf-8")
        (state_root / "session.json").write_text("{}", encoding="utf-8")
        now = datetime(2026, 8, 31, tzinfo=timezone.utc)
        database.touch_session(
            session.id, at=(now - timedelta(days=15)).isoformat()
        )

        assert await lifecycle.cleanup_expired_once(now=now) == (session.id,)
        assert not workspace.session_dir(session.id).exists()
        assert database.inactive_session_ids(now.isoformat()) == ()
        await runtime.shutdown()

    asyncio.run(scenario())


def test_recent_session_is_retained(tmp_path: Path) -> None:
    async def scenario() -> None:
        _settings, database, workspace, runtime, lifecycle = make_services(tmp_path)
        user, _ = database.get_or_create_user(None)
        session = database.ensure_session(user.id)
        workspace.ensure_session_directories(session.id)
        now = datetime(2026, 8, 31, tzinfo=timezone.utc)
        database.touch_session(
            session.id, at=(now - timedelta(days=13)).isoformat()
        )

        assert await lifecycle.cleanup_expired_once(now=now) == ()
        assert workspace.session_dir(session.id).exists()
        assert database.inactive_session_ids(now.isoformat()) == (session.id,)
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
    settings = ServerSettings(data_dir=tmp_path / "data", relay_token="token")
    launcher = StartupLauncher()
    app = create_app(settings, launcher=launcher)
    shutdown = AsyncMock()
    app.state.services.runtime.shutdown = shutdown

    with TestClient(app) as client:
        assert client.get("/mycode/api/health").status_code == 200

    assert launcher.cleanup_calls == 1
    shutdown.assert_awaited_once_with()


def test_application_startup_logs_cleanup_failure_and_continues(
    tmp_path: Path, caplog
) -> None:
    settings = ServerSettings(data_dir=tmp_path / "data", relay_token="token")
    launcher = FailingCleanupLauncher()
    app = create_app(settings, launcher=launcher)
    caplog.set_level("ERROR")

    with TestClient(app) as client:
        assert client.get("/mycode/api/health").status_code == 200

    assert launcher.cleanup_calls == 1
    assert "startup cleanup failed; startup will continue" in caplog.text
