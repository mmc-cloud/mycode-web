import asyncio
from pathlib import Path

from app.config import ServerSettings
from app.db.database import CONSOLE_HISTORY_LIMIT, WebDatabase
from app.services.console import ConsoleRecorder
from app.services.events import EventHub


def make_database(tmp_path: Path) -> tuple[WebDatabase, str, str]:
    settings = ServerSettings(data_dir=tmp_path / "data")
    settings.ensure_directories()
    database = WebDatabase(settings.database_path)
    database.initialize()
    user, _ = database.get_or_create_user(None)
    return database, database.create_session(user.id).id, user.id


def test_console_history_is_bounded_structured_and_session_isolated(
    tmp_path: Path,
) -> None:
    database, session_a, user_id = make_database(tmp_path)
    session_b = database.create_session(user_id).id
    recorder = ConsoleRecorder(database)
    recorder.record_event(
        session_a,
        "agent_event",
        {"turn_id": "turn-1", "event": {"type": "text_delta", "content": "hello"}},
    )
    recorder.record_event(session_a, "turn_finished", {"turn_id": "turn-1"})
    recorder.record_event(
        session_a,
        "permission_request",
        {"tool_name": "write_file", "action": "write", "target": "a.txt"},
    )
    recorder.record_event(session_a, "user_message", {"content": "hello"})
    recorder.record_event(
        session_a,
        "agent_event",
        {"turn_id": "turn-2", "event": {"type": "error", "error": "failed"}},
    )
    recorder.record_event(session_b, "user_message", {"content": "only b"})
    for index in range(CONSOLE_HISTORY_LIMIT + 20):
        database.append_console_event(session_a, "tool", f"event {index}")

    history_a = database.console_history(session_a, user_id)
    history_b = database.console_history(session_b, user_id)
    assert len(history_a) == CONSOLE_HISTORY_LIMIT
    assert history_a[-1].content == f"event {CONSOLE_HISTORY_LIMIT + 19}"
    assert [event.content for event in history_b] == ["only b"]
    assert all(event.kind in {"assistant", "permission", "user", "error", "tool"} for event in history_a)


def test_live_output_is_structured_and_flushes_on_turn_finished(tmp_path: Path) -> None:
    database, session_id, user_id = make_database(tmp_path)
    recorder = ConsoleRecorder(database)

    assert recorder.record_event(
        session_id,
        "agent_event",
        {"turn_id": "turn-1", "event": {"type": "text_delta", "content": "hello"}},
    ) == ()
    assert recorder.live_output(session_id) == {
        "active": True,
        "kind": "assistant",
        "content": "hello",
        "turn_id": "turn-1",
    }
    recorder.record_event(
        session_id,
        "agent_event",
        {"turn_id": "turn-1", "event": {"type": "text_delta", "content": " world"}},
    )
    recorded = recorder.record_event(session_id, "turn_finished", {"turn_id": "turn-1"})
    assert len(recorded) == 1
    assert recorder.live_output(session_id)["active"] is False
    history = database.console_history(session_id, user_id)
    assert len(history) == 1
    assert history[0].content == "hello world"
    assert history[0].data == {"turn_id": "turn-1", "event_type": "text_delta"}


def test_console_permission_resolution_keeps_decision_scope(tmp_path: Path) -> None:
    database, session_id, user_id = make_database(tmp_path)
    recorder = ConsoleRecorder(database)

    recorded = recorder.record_event(
        session_id,
        "permission_resolved",
        {"decision": "task", "allowed": True},
    )

    assert recorded[0].content == "已允许（当前任务）"
    history = database.console_history(session_id, user_id)
    assert history[0].data == {"decision": "task", "allowed": True}


def test_event_hub_sends_structured_live_console_before_persisting_line(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database, session_id, user_id = make_database(tmp_path)
        hub = EventHub(console=ConsoleRecorder(database))
        stream = hub.stream(session_id, hub.latest_id(session_id))

        first = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        await hub.publish(
            session_id,
            "agent_event",
            turn_id="turn-1",
            event={"type": "text_delta", "content": "hello"},
        )
        assert (await first).type == "agent_event"
        live = await anext(stream)
        assert live.type == "console_live"
        assert live.data["content"] == "hello"
        assert database.console_history(session_id, user_id) == ()

        await hub.publish(
            session_id,
            "agent_event",
            turn_id="turn-1",
            event={"type": "text_delta", "content": " world"},
        )
        assert (await anext(stream)).type == "agent_event"
        live = await anext(stream)
        assert live.type == "console_live"
        assert live.data["content"] == "hello world"
        assert database.console_history(session_id, user_id) == ()

        await hub.publish(session_id, "turn_finished", turn_id="turn-1")
        assert (await anext(stream)).type == "turn_finished"
        cleared = await anext(stream)
        persisted = await anext(stream)
        assert cleared.type == "console_live"
        assert cleared.data["active"] is False
        assert persisted.type == "console_event"
        history = database.console_history(session_id, user_id)
        assert len(history) == 1
        assert history[0].content == "hello world"
        await stream.aclose()

    asyncio.run(scenario())
