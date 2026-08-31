from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
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
                    user_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    last_active_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES web_users(id) ON DELETE CASCADE
                );
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

    def ensure_session(self, user_id: str) -> WebSession:
        now = _utc_now()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM web_sessions WHERE user_id = ?", (user_id,)
            ).fetchone()
            if row is None:
                session_id = secrets.token_urlsafe(24)
                connection.execute(
                    "INSERT INTO web_sessions VALUES (?, ?, ?, ?)",
                    (session_id, user_id, now, now),
                )
                return WebSession(session_id, user_id, now, now)
            connection.execute(
                "UPDATE web_sessions SET last_active_at = ? WHERE id = ?",
                (now, row["id"]),
            )
            return _session_from_row(row, last_active_at=now)

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

    def delete_session(self, session_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM web_sessions WHERE id = ?", (session_id,)
            )
        return cursor.rowcount == 1

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
