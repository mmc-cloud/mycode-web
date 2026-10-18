import asyncio
import logging
from pathlib import Path

import pytest

from app.services.events import EventHub
from app.services.runtime import RuntimeManager
from app.services.workspace import WorkspaceService
from test_runtime import FakeLauncher, settings, wait_for_status


@pytest.mark.parametrize(("received", "reason", "logged_id"), [
    (None, "missing_turn_id", None),
    ("", "invalid_turn_id", ""),
    (" ", "invalid_turn_id", " "),
    (123, "invalid_turn_id", "<int>"),
    ("unknown-turn", "turn_id_mismatch", "unknown-turn"),
    ("unknown\nturn", "turn_id_mismatch", "unknown turn"),
])
def test_protocol_warning_identifies_bad_completion_without_changing_current_turn(
    tmp_path: Path, caplog, received, reason: str, logged_id
) -> None:
    async def scenario() -> None:
        config = settings(tmp_path)
        manager = RuntimeManager(config, WorkspaceService(config), EventHub(), FakeLauncher())
        await manager.send_message("session", "task", turn_id="current-turn")
        state = manager._sessions["session"]
        state.pending_permission = {"request_id": "pending"}
        state.status = "waiting_permission"
        caplog.clear()
        before = manager.events.history("session")
        await manager._handle_message("session", state, {
            "type": "turn_finished", "turn_id": received, "status": "completed",
        })
        records = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(records) == 1
        record = records[0]
        assert record.protocol_code == "unexpected_turn_finished"
        assert record.reason == reason
        assert record.received_turn_id == logged_id
        assert record.active_turn_id == "current-turn"
        assert record.runtime_generation == 1
        assert record.session_id == "session"
        assert reason in record.getMessage()
        assert "current-turn" in record.getMessage()
        assert "\n" not in record.getMessage()
        assert manager.events.history("session") == before
        assert state.status == "waiting_permission"
        assert state.active_turn_id == "current-turn"
        assert state.busy
        assert state.pending_permission == {"request_id": "pending"}
        assert not state.completed_turn_ids
        await manager._handle_message("session", state, {
            "type": "turn_finished", "turn_id": "current-turn", "status": "completed",
        })
        assert state.status == "idle"
        assert list(state.completed_turn_ids) == ["current-turn"]
        await manager.shutdown()

    caplog.set_level(logging.WARNING, logger="app.services.runtime")
    asyncio.run(scenario())


@pytest.mark.parametrize("next_turn", [False, True])
def test_known_duplicate_completion_is_debug_only_with_or_without_another_active_turn(
    tmp_path: Path, caplog, next_turn: bool
) -> None:
    async def scenario() -> None:
        config = settings(tmp_path)
        manager = RuntimeManager(config, WorkspaceService(config), EventHub(), FakeLauncher())
        await manager.send_message("session", "first", turn_id="finished-turn")
        state = manager._sessions["session"]
        completed = {"type": "turn_finished", "turn_id": "finished-turn", "status": "completed"}
        await manager._handle_message("session", state, completed)
        if next_turn:
            await manager.send_message("session", "second", turn_id="current-turn")
        before = manager.events.history("session")
        caplog.clear()
        await manager._handle_message("session", state, completed)
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        record, = [r for r in caplog.records if getattr(r, "reason", None) == "duplicate_turn_finished"]
        assert record.levelno == logging.DEBUG
        assert record.received_turn_id == "finished-turn"
        assert state.active_turn_id == ("current-turn" if next_turn else None)
        assert state.status == ("running" if next_turn else "idle")
        assert manager.events.history("session") == before
        await manager.shutdown()

    caplog.set_level(logging.DEBUG, logger="app.services.runtime")
    asyncio.run(scenario())


def test_late_completion_during_finalization_is_debug_and_cannot_reopen_runtime(tmp_path: Path, caplog) -> None:
    async def scenario() -> None:
        config = settings(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()

        async def stop_hook(*_args):
            entered.set()
            await release.wait()

        manager = RuntimeManager(config, WorkspaceService(config), EventHub(), FakeLauncher(),
                                 runtime_stop_hook=stop_hook)
        await manager.send_message("session", "first", turn_id="current-turn")
        stopping = asyncio.create_task(manager.stop_session("session"))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            before = manager.events.history("session")
            caplog.clear()
            state = manager._sessions["session"]
            await manager._handle_message("session", state, {
                "type": "turn_finished", "turn_id": "current-turn", "status": "completed",
            })
            assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
            record, = [r for r in caplog.records if getattr(r, "reason", None) == "runtime_finalizing"]
            assert record.levelno == logging.DEBUG
            assert state.status == "stopping"
            assert state.active_turn_id == "current-turn"
            assert not state.completed_turn_ids
            assert manager.events.history("session") == before
        finally:
            release.set()
            await stopping
            await manager.shutdown()

    caplog.set_level(logging.DEBUG, logger="app.services.runtime")
    asyncio.run(scenario())


@pytest.mark.parametrize("reserved", [False, True])
def test_unsolicited_or_not_started_turn_has_distinct_warning(tmp_path: Path, caplog, reserved: bool) -> None:
    async def scenario() -> None:
        config = settings(tmp_path)
        manager = RuntimeManager(config, WorkspaceService(config), EventHub(), FakeLauncher())
        await manager.activate("session")
        await wait_for_status(manager, "session", "idle")
        state = manager._sessions["session"]
        if reserved:
            state.active_turn_id = "reserved-turn"
        caplog.clear()
        await manager._handle_message("session", state, {
            "type": "turn_finished", "turn_id": "reserved-turn", "status": "completed",
        })
        record, = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert record.reason == ("turn_not_running" if reserved else "no_active_turn")
        assert state.status == "idle"
        assert state.active_turn_id == ("reserved-turn" if reserved else None)
        await manager.shutdown()

    caplog.set_level(logging.WARNING, logger="app.services.runtime")
    asyncio.run(scenario())


def test_completed_turn_diagnostics_are_bounded_and_reset_for_next_runtime_generation(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = settings(tmp_path)
        manager = RuntimeManager(config, WorkspaceService(config), EventHub(), FakeLauncher())
        for index in range(130):
            turn_id = f"turn-{index}"
            await manager.send_message("session", "task", turn_id=turn_id)
            await manager._handle_message("session", manager._sessions["session"], {
                "type": "turn_finished", "turn_id": turn_id, "status": "completed",
            })
        state = manager._sessions["session"]
        assert list(state.completed_turn_ids) == [f"turn-{index}" for index in range(2, 130)]
        await manager.stop_session("session")
        await manager.activate("session")
        await wait_for_status(manager, "session", "idle")
        assert state.runtime_generation == 2
        assert not state.completed_turn_ids
        await manager.shutdown()

    asyncio.run(scenario())
