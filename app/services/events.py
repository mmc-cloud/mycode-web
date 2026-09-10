import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import AsyncIterator

from app.services.console import ConsoleRecorder


@dataclass(frozen=True)
class WebEvent:
    id: int
    type: str
    data: dict[str, object]
    created_at: str


@dataclass(eq=False)
class _Subscriber:
    queue: asyncio.Queue[WebEvent | None]


class _SessionEvents:
    def __init__(self, history_limit: int) -> None:
        self.condition = asyncio.Condition()
        self.history: deque[WebEvent] = deque(maxlen=history_limit)
        self.next_id = 1
        self.subscribers: set[_Subscriber] = set()


class EventHub:
    def __init__(
        self,
        history_limit: int = 1000,
        *,
        console: ConsoleRecorder | None = None,
        subscriber_queue_limit: int = 256,
    ) -> None:
        if subscriber_queue_limit < 1:
            raise ValueError("subscriber_queue_limit must be positive")
        self.history_limit = history_limit
        self.console = console
        self.subscriber_queue_limit = subscriber_queue_limit
        self._sessions: dict[str, _SessionEvents] = {}

    def _state(self, session_id: str) -> _SessionEvents:
        return self._sessions.setdefault(
            session_id, _SessionEvents(self.history_limit)
        )

    async def publish(
        self, session_id: str, event_type: str, **data: object
    ) -> WebEvent:
        replayable = event_type not in {"agent_event", "console_live"}
        event = await self._publish(
            session_id, event_type, dict(data), replayable=replayable
        )
        if self.console is not None:
            console_events = self.console.record_event(
                session_id, event_type, dict(data)
            )
            if event_type in {"agent_event", "turn_finished"}:
                await self._publish(
                    session_id,
                    "console_live",
                    self.console.live_output(session_id),
                    replayable=False,
                )
            for console_event in console_events:
                await self._publish(
                    session_id,
                    "console_event",
                    {
                        "console_id": console_event.id,
                        "kind": console_event.kind,
                        "content": console_event.content,
                        "data": console_event.data,
                    },
                )
        return event

    async def _publish(
        self,
        session_id: str,
        event_type: str,
        data: dict[str, object],
        *,
        replayable: bool = True,
    ) -> WebEvent:
        state = self._state(session_id)
        async with state.condition:
            event = WebEvent(
                id=state.next_id,
                type=event_type,
                data=data,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            state.next_id += 1
            if replayable:
                state.history.append(event)
            for subscriber in tuple(state.subscribers):
                try:
                    subscriber.queue.put_nowait(event)
                except asyncio.QueueFull:
                    if not replayable:
                        # Live-only deltas are intentionally lossy. A reconnect
                        # obtains the stable history from the replay buffer.
                        continue
                    # Stable events are already in history. Close this slow
                    # stream so EventSource reconnects with Last-Event-ID.
                    state.subscribers.discard(subscriber)
                    while True:
                        try:
                            subscriber.queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    subscriber.queue.put_nowait(None)
            state.condition.notify_all()
            return event

    def remove_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        if self.console is not None:
            self.console.clear_session(session_id)

    def history(self, session_id: str) -> tuple[WebEvent, ...]:
        return tuple(self._state(session_id).history)

    def latest_id(self, session_id: str) -> int:
        return self._state(session_id).next_id - 1

    async def stream(
        self, session_id: str, after_id: int = 0
    ) -> AsyncIterator[WebEvent]:
        state = self._state(session_id)
        cursor = max(after_id, 0)
        subscriber = _Subscriber(
            asyncio.Queue(maxsize=self.subscriber_queue_limit)
        )
        async with state.condition:
            available = [event for event in state.history if event.id > cursor]
            state.subscribers.add(subscriber)
        try:
            for event in available:
                cursor = event.id
                yield event
            while True:
                try:
                    event = await asyncio.wait_for(subscriber.queue.get(), timeout=15)
                except TimeoutError:
                    yield WebEvent(
                        id=cursor,
                        type="keepalive",
                        data={},
                        created_at=datetime.now(timezone.utc).isoformat(),
                    )
                    continue
                if event is None:
                    return
                if event.id > cursor:
                    cursor = event.id
                    yield event
        finally:
            state.subscribers.discard(subscriber)


def encode_sse(event: WebEvent) -> str:
    if event.type == "keepalive":
        return ": keepalive\n\n"
    payload = {**event.data, "created_at": event.created_at}
    return (
        f"id: {event.id}\n"
        f"event: {event.type}\n"
        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    )
