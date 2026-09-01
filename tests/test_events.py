import asyncio

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


def test_raw_agent_output_is_live_only_not_replay_history() -> None:
    async def scenario() -> None:
        hub = EventHub()
        stream = hub.stream("session", hub.latest_id("session"))
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        published = await hub.publish("session", "agent_output", content="chunk")
        assert await asyncio.wait_for(pending, timeout=1) == published
        assert hub.history("session") == ()
        await stream.aclose()

    asyncio.run(scenario())
