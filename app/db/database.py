from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import sqlite3
from threading import RLock


@dataclass(frozen=True)
class WebUser:
    id: str
    display_name: str | None
    created_at: str
    last_active_at: str


@dataclass(frozen=True)
class WebSession:
    id: str
    user_id: str
    created_at: str
    last_active_at: str


@dataclass(frozen=True)
class ConsoleEvent:
    id: int
    session_id: str
    kind: str
    content: str
    data: dict[str, object]
    created_at: str


CONSOLE_HISTORY_LIMIT = 500
CONSOLE_EVENT_MAX_CHARS = 64_000


class WebDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = RLock()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS web_users (
                    id TEXT PRIMARY KEY,
                    display_name TEXT,
                    created_at TEXT NOT NULL,
                    last_active_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS web_sessions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_active_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES web_users(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_web_sessions_user_activity
                    ON web_sessions(user_id, last_active_at DESC);
                CREATE TABLE IF NOT EXISTS console_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES web_sessions(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_console_events_session_id
                    ON console_events(session_id, id DESC);
                """
            )

    def get_or_create_user(self, candidate_id: str | None) -> tuple[WebUser, bool]:
        now = _utc_now()
        with self._lock, self._connect() as connection:
            if candidate_id:
                row = connection.execute(
                    "SELECT * FROM web_users WHERE id = ?", (candidate_id,)
                ).fetchone()
                if row is not None:
                    connection.execute(
                        "UPDATE web_users SET last_active_at = ? WHERE id = ?",
                        (now, candidate_id),
                    )
                    return _user_from_row(row, last_active_at=now), False

            user_id = secrets.token_urlsafe(32)
            connection.execute(
                "INSERT INTO web_users VALUES (?, NULL, ?, ?)",
                (user_id, now, now),
            )
            return WebUser(user_id, None, now, now), True

    def create_session(self, user_id: str) -> WebSession:
        now = _utc_now()
        with self._lock, self._connect() as connection:
            session_id = secrets.token_urlsafe(24)
            connection.execute(
                "INSERT INTO web_sessions VALUES (?, ?, ?, ?)",
                (session_id, user_id, now, now),
            )
        return WebSession(session_id, user_id, now, now)

    def list_sessions(self, user_id: str) -> tuple[WebSession, ...]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM web_sessions WHERE user_id = ? "
                "ORDER BY last_active_at DESC, created_at DESC",
                (user_id,),
            ).fetchall()
        return tuple(_session_from_row(row) for row in rows)

    def get_session(self, session_id: str, user_id: str) -> WebSession | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM web_sessions WHERE id = ? AND user_id = ?",
                (session_id, user_id),
            ).fetchone()
        return None if row is None else _session_from_row(row)

    def update_display_name(self, user_id: str, display_name: str) -> WebUser:
        normalized = display_name.strip()
        if not normalized or len(normalized) > 80:
            raise ValueError("display_name must contain 1 to 80 characters.")
        now = _utc_now()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE web_users SET display_name = ?, last_active_at = ? WHERE id = ?",
                (normalized, now, user_id),
            )
            if cursor.rowcount != 1:
                raise LookupError("Web user no longer exists.")
            row = connection.execute(
                "SELECT * FROM web_users WHERE id = ?", (user_id,)
            ).fetchone()
        return _user_from_row(row)

    def touch_session(self, session_id: str, *, at: str | None = None) -> None:
        timestamp = at or _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE web_sessions SET last_active_at = ? WHERE id = ?",
                (timestamp, session_id),
            )

    def inactive_session_ids(self, cutoff: str) -> tuple[str, ...]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT id FROM web_sessions "
                "WHERE last_active_at < ? ORDER BY last_active_at, id",
                (cutoff,),
            ).fetchall()
        return tuple(row["id"] for row in rows)

    def delete_session(self, session_id: str, *, user_id: str | None = None) -> bool:
        with self._lock, self._connect() as connection:
            if user_id is None:
                cursor = connection.execute(
                    "DELETE FROM web_sessions WHERE id = ?", (session_id,)
                )
            else:
                cursor = connection.execute(
                    "DELETE FROM web_sessions WHERE id = ? AND user_id = ?",
                    (session_id, user_id),
                )
        return cursor.rowcount == 1

    def append_console_event(
        self,
        session_id: str,
        kind: str,
        content: str,
        *,
        data: dict[str, object] | None = None,
        coalesce: bool = False,
    ) -> ConsoleEvent | None:
        normalized = content.strip("\r\n")
        if not normalized:
            return None
        timestamp = _utc_now()
        payload = json.dumps(data or {}, ensure_ascii=False)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM console_events WHERE session_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            if (
                coalesce
                and row is not None
                and row["kind"] == kind
                and len(row["content"]) + len(normalized) <= CONSOLE_EVENT_MAX_CHARS
            ):
                content_value = row["content"] + "\n" + normalized
                connection.execute(
                    "UPDATE console_events SET content = ?, created_at = ? WHERE id = ?",
                    (content_value, timestamp, row["id"]),
                )
                event_id = row["id"]
            else:
                content_value = normalized[:CONSOLE_EVENT_MAX_CHARS]
                cursor = connection.execute(
                    "INSERT INTO console_events "
                    "(session_id, kind, content, data_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (session_id, kind, content_value, payload, timestamp),
                )
                event_id = int(cursor.lastrowid)
            connection.execute(
                "DELETE FROM console_events WHERE session_id = ? AND id NOT IN "
                "(SELECT id FROM console_events WHERE session_id = ? "
                "ORDER BY id DESC LIMIT ?)",
                (session_id, session_id, CONSOLE_HISTORY_LIMIT),
            )
        return ConsoleEvent(
            id=event_id,
            session_id=session_id,
            kind=kind,
            content=content_value,
            data=data or {},
            created_at=timestamp,
        )

    def console_history(
        self, session_id: str, user_id: str, *, limit: int = CONSOLE_HISTORY_LIMIT
    ) -> tuple[ConsoleEvent, ...]:
        bounded_limit = max(1, min(limit, CONSOLE_HISTORY_LIMIT))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT event.* FROM console_events AS event "
                "JOIN web_sessions AS session ON session.id = event.session_id "
                "WHERE event.session_id = ? AND session.user_id = ? "
                "ORDER BY event.id DESC LIMIT ?",
                (session_id, user_id, bounded_limit),
            ).fetchall()
        return tuple(_console_event_from_row(row) for row in reversed(rows))

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _user_from_row(
    row: sqlite3.Row, *, last_active_at: str | None = None
) -> WebUser:
    return WebUser(
        id=row["id"],
        display_name=row["display_name"],
        created_at=row["created_at"],
        last_active_at=last_active_at or row["last_active_at"],
    )


def _session_from_row(
    row: sqlite3.Row, *, last_active_at: str | None = None
) -> WebSession:
    return WebSession(
        id=row["id"],
        user_id=row["user_id"],
        created_at=row["created_at"],
        last_active_at=last_active_at or row["last_active_at"],
    )


def _console_event_from_row(row: sqlite3.Row) -> ConsoleEvent:
    return ConsoleEvent(
        id=row["id"],
        session_id=row["session_id"],
        kind=row["kind"],
        content=row["content"],
        data=json.loads(row["data_json"]),
        created_at=row["created_at"],
    )
