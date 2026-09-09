import asyncio

import pytest

from app.services.events import EventHub, encode_sse


def test_reconnect_replays_stable_events_and_encodes_them() -> None:
    async def scenario() -> None:
        hub = EventHub()
        published = await hub.publish("session", "runtime_status", status="idle")
        streamed = await anext(hub.stream("session"))
        assert streamed == published
        assert "event: runtime_status" in encode_sse(streamed)
        assert '"status": "idle"' in encode_sse(streamed)

    asyncio.run(scenario())


def test_sse_consumer_does_not_hold_publish_lock() -> None:
    async def scenario() -> None:
        hub = EventHub()
        stream = hub.stream("session")
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        await hub.publish("session", "runtime_status", status="idle")
        event = await asyncio.wait_for(pending, timeout=1)
        assert event.type == "runtime_status"
        await stream.aclose()

    asyncio.run(scenario())


def test_sse_history_is_strictly_session_scoped() -> None:
    async def scenario() -> None:
        hub = EventHub()
        await hub.publish("a", "runtime_status", status="only-a")
        await hub.publish("b", "runtime_status", status="only-b")
        assert [event.data["status"] for event in hub.history("a")] == ["only-a"]
        assert [event.data["status"] for event in hub.history("b")] == ["only-b"]

    asyncio.run(scenario())


def test_fresh_cursor_skips_history_but_reconnect_replays_missed_events() -> None:
    async def scenario() -> None:
        hub = EventHub()
        first = await hub.publish("session", "runtime_status", status="old")
        fresh = hub.stream("session", hub.latest_id("session"))
        pending = asyncio.create_task(anext(fresh))
        await asyncio.sleep(0)
        current = await hub.publish("session", "runtime_status", status="current")
        assert await asyncio.wait_for(pending, timeout=1) == current
        await fresh.aclose()

        missed = await hub.publish("session", "runtime_status", status="missed")
        reconnect = hub.stream("session", first.id)
        assert await anext(reconnect) == current
        assert await anext(reconnect) == missed
        await reconnect.aclose()

    asyncio.run(scenario())


def test_structured_agent_event_is_live_only_not_replayed() -> None:
    async def scenario() -> None:
        hub = EventHub()
        stream = hub.stream("session", hub.latest_id("session"))
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        published = await hub.publish(
            "session",
            "agent_event",
            turn_id="turn-1",
            event={"type": "text_delta", "content": "chunk"},
        )
        assert await asyncio.wait_for(pending, timeout=1) == published
        assert hub.history("session") == ()
        await stream.aclose()

    asyncio.run(scenario())


def test_live_only_events_are_bounded_and_stable_overflow_closes_stream() -> None:
    async def scenario() -> None:
        hub = EventHub(subscriber_queue_limit=1)
        stream = hub.stream("session", hub.latest_id("session"))
        first_pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        first = await hub.publish(
            "session", "agent_event", event={"type": "text_delta", "content": "1"}
        )
        assert await asyncio.wait_for(first_pending, timeout=1) == first

        await hub.publish(
            "session", "agent_event", event={"type": "text_delta", "content": "2"}
        )
        await hub.publish(
            "session", "agent_event", event={"type": "text_delta", "content": "3"}
        )
        subscriber = next(iter(hub._sessions["session"].subscribers))
        assert subscriber.queue.qsize() <= 1

        stable = await hub.publish("session", "runtime_status", status="idle")
        assert stable in hub.history("session")
        with pytest.raises(StopAsyncIteration):
            await anext(stream)

    asyncio.run(scenario())
