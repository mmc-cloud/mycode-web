import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import AsyncIterator


@dataclass(frozen=True)
class WebEvent:
    id: int
    type: str
    data: dict[str, object]
    created_at: str


class _SessionEvents:
    def __init__(self, history_limit: int) -> None:
        self.condition = asyncio.Condition()
        self.history: deque[WebEvent] = deque(maxlen=history_limit)
        self.next_id = 1


class EventHub:
    def __init__(self, history_limit: int = 1000) -> None:
        self.history_limit = history_limit
        self._sessions: dict[str, _SessionEvents] = {}

    def _state(self, session_id: str) -> _SessionEvents:
        return self._sessions.setdefault(
            session_id, _SessionEvents(self.history_limit)
        )

    async def publish(
        self, session_id: str, event_type: str, **data: object
    ) -> WebEvent:
        state = self._state(session_id)
        async with state.condition:
            event = WebEvent(
                id=state.next_id,
                type=event_type,
                data=dict(data),
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            state.next_id += 1
            state.history.append(event)
            state.condition.notify_all()
            return event

    def history(self, session_id: str) -> tuple[WebEvent, ...]:
        return tuple(self._state(session_id).history)

    async def stream(
        self, session_id: str, after_id: int = 0
    ) -> AsyncIterator[WebEvent]:
        state = self._state(session_id)
        cursor = max(after_id, 0)
        while True:
            keepalive: WebEvent | None = None
            async with state.condition:
                available = [event for event in state.history if event.id > cursor]
                if not available:
                    try:
                        await asyncio.wait_for(state.condition.wait(), timeout=15)
                    except TimeoutError:
                        keepalive = WebEvent(
                            id=cursor,
                            type="keepalive",
                            data={},
                            created_at=datetime.now(timezone.utc).isoformat(),
                        )
                    if keepalive is None:
                        available = [
                            event for event in state.history if event.id > cursor
                        ]
            if keepalive is not None:
                yield keepalive
                continue
            for event in available:
                cursor = event.id
                yield event


def encode_sse(event: WebEvent) -> str:
    if event.type == "keepalive":
        return ": keepalive\n\n"
    payload = {**event.data, "created_at": event.created_at}
    return (
        f"id: {event.id}\n"
        f"event: {event.type}\n"
        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    )
