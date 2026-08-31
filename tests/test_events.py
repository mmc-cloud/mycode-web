import asyncio

from app.services.events import EventHub, encode_sse


def test_sse_event_is_replayable_and_encoded() -> None:
    async def scenario() -> None:
        hub = EventHub()
        published = await hub.publish("session", "agent_output", content="hello")
        streamed = await anext(hub.stream("session"))
        assert streamed == published
        assert "event: agent_output" in encode_sse(streamed)
        assert '"content": "hello"' in encode_sse(streamed)

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
