from pathlib import Path

from app.config import ServerSettings
from app.db.database import WebDatabase
from app.services.console import ConsoleRecorder


def make_database(tmp_path: Path) -> tuple[WebDatabase, str, str]:
    settings = ServerSettings(data_dir=tmp_path / "data", relay_token="token")
    settings.ensure_directories()
    database = WebDatabase(settings.database_path)
    database.initialize()
    user, _ = database.get_or_create_user(None)
    return database, database.create_session(user.id).id, user.id


def test_console_events_persist_turn_id_and_do_not_coalesce_across_turns(
    tmp_path: Path,
) -> None:
    database, session_id, user_id = make_database(tmp_path)
    recorder = ConsoleRecorder(database)

    recorder.record_event(
        session_id, "user_message", {"content": "first", "turn_id": "turn-1"}
    )
    recorder.record_event(
        session_id, "agent_output", {"content": "assistant> one\n", "turn_id": "turn-1"}
    )
    recorder.record_event(
        session_id, "agent_output", {"content": "assistant> two\n", "turn_id": "turn-2"}
    )

    history = database.console_history(session_id, user_id)
    assert history[0].data["turn_id"] == "turn-1"
    assert history[1].data["turn_id"] == "turn-1"
    assert history[2].data["turn_id"] == "turn-2"
    assert history[1].content == "one"
    assert history[2].content == "two"


def test_old_console_rows_without_turn_id_are_safe(tmp_path: Path) -> None:
    database, session_id, user_id = make_database(tmp_path)
    database.append_console_event(session_id, "assistant", "legacy")

    history = database.console_history(session_id, user_id)

    assert history[0].data == {}
