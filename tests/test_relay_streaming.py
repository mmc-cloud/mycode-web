import asyncio
import gzip
import json
import logging
from pathlib import Path
import zlib

import httpx
import pytest
from fastapi.testclient import TestClient
from fastapi.responses import StreamingResponse

from app.config import ServerSettings
from app.main import create_app
from app.services.relay import LLMRelay


PROVIDER_SECRET = "provider-key-do-not-log"
PROMPT = b'{"messages":[{"role":"user","content":"private-prompt"}],"tools":[{"private":"private-tool"}],"stream":true}'
FIRST_EVENT = b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
SECOND_EVENT = b'data: {"choices":[{"delta":{"content":" World"}}]}\n\n'
DONE = b"data: [DONE]\n\n"
SSE = FIRST_EVENT + SECOND_EVENT + DONE


class RecordingStream(httpx.AsyncByteStream):
    def __init__(self, chunks, *, failure=None, block_after=None):
        self.chunks = chunks
        self.failure = failure
        self.block_after = block_after
        self.waiting = asyncio.Event()
        self.resume = asyncio.Event()
        self.read_count = 0
        self.close_count = 0
        self.exhausted = False

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.read_count == self.block_after:
                self.waiting.set()
                await self.resume.wait()
            self.read_count += 1
            yield chunk
        if self.failure is not None:
            raise self.failure
        self.exhausted = True

    async def aclose(self):
        self.close_count += 1


def encode(body, encoding):
    if encoding == "gzip":
        return gzip.compress(body, mtime=0)
    if encoding == "deflate":
        return zlib.compress(body)
    return body


def gzip_events():
    # Fixture generation only. Relay itself uses HTTPX, not a custom decoder.
    compressor = zlib.compressobj(wbits=zlib.MAX_WBITS | 16)
    first = compressor.compress(FIRST_EVENT) + compressor.flush(zlib.Z_SYNC_FLUSH)
    second = compressor.compress(SECOND_EVENT) + compressor.flush(zlib.Z_SYNC_FLUSH)
    last = compressor.compress(DONE) + compressor.flush(zlib.Z_FINISH)
    assert first[:2] == b"\x1f\x8b"
    return [first[:1], first[1:2], first[2:], second, last]


def make_relay(tmp_path, chunks, *, status=200, headers=None,
               failure=None, block_after=None):
    stream = RecordingStream(chunks, failure=failure, block_after=block_after)
    requests, responses = [], []

    def handler(request):
        requests.append(request)
        response = httpx.Response(
            status, headers=headers or {"Content-Type": "text/event-stream"},
            stream=stream,
        )
        responses.append(response)
        return response

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False,
    )
    relay = LLMRelay(
        ServerSettings(
            data_dir=tmp_path, provider_base_url="https://provider.invalid/v1",
            provider_api_key=PROVIDER_SECRET,
        ), client_factory=lambda: client,
    )
    return relay, stream, requests, responses, client


async def forward(relay):
    await relay.start()
    return await relay.forward(
        "chat/completions", PROMPT, "application/json", session_id="session-a",
    )


@pytest.mark.parametrize("encoding", [None, "gzip", "deflate"])
@pytest.mark.parametrize("split", ["whole", "header", "every_byte"])
def test_sse_decodes_through_real_httpx_and_preserves_body_headers_and_lifetime(
    tmp_path: Path, caplog, encoding, split,
):
    caplog.set_level(logging.DEBUG, logger="app.services.relay")
    raw = encode(SSE, encoding)
    if encoding == "gzip":
        assert raw[:2] == b"\x1f\x8b"
    chunks = {
        "whole": [raw],
        "header": [b"", raw[:1], b"", raw[1:2], raw[2:]],
        "every_byte": [raw[i:i + 1] for i in range(len(raw))],
    }[split]
    headers = {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-store", "X-Request-Id": "request-123",
        "Retry-After": "7", "Content-Length": str(len(raw)),
        "Transfer-Encoding": "chunked", "Connection": "keep-alive",
        "Set-Cookie": "private-cookie", "Authorization": PROVIDER_SECRET,
    }
    if encoding:
        headers["Content-Encoding"] = encoding
    relay, stream, requests, responses, client = make_relay(
        tmp_path, chunks, headers=headers,
    )

    async def scenario():
        status, forwarded, output = await forward(relay)
        assert stream.read_count == 0  # headers do not trigger eager body reading
        assert b"".join([chunk async for chunk in output]) == SSE
        assert status == 200
        assert forwarded == {
            "content-type": "text/event-stream; charset=utf-8",
            "cache-control": "no-store", "x-request-id": "request-123",
            "retry-after": "7",
        }
        assert len(requests) == 1 and requests[0].content == PROMPT
        assert requests[0].headers["authorization"] == f"Bearer {PROVIDER_SECRET}"
        assert responses[0].is_closed and stream.close_count == 1
        assert not client.is_closed
        await relay.aclose()
        assert client.is_closed

    asyncio.run(scenario())
    assert not [record for record in caplog.records if record.name == "app.services.relay"]
    for secret in (PROVIDER_SECRET, "private-prompt", "private-tool", "private-cookie", "Hello"):
        assert secret not in caplog.text


def test_gzip_outputs_each_sse_event_before_upstream_finishes(tmp_path: Path):
    relay, stream, _, responses, _ = make_relay(
        tmp_path, gzip_events(), headers={"Content-Encoding": "gzip"},
    )

    async def scenario():
        _, _, output = await forward(relay)
        assert await anext(output) == FIRST_EVENT
        assert stream.read_count == 3 and not stream.exhausted
        assert not responses[0].is_closed
        assert await anext(output) == SECOND_EVENT
        assert stream.read_count == 4 and not stream.exhausted
        assert b"".join([chunk async for chunk in output]) == DONE
        assert stream.exhausted and stream.close_count == 1
        await relay.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [429, 500, 503])
@pytest.mark.parametrize("encoding", [None, "gzip", "deflate"])
def test_compressed_json_errors_keep_original_status_and_retry_headers(
    tmp_path: Path, status, encoding,
):
    body = json.dumps({"error": {"message": "provider error", "code": status}}).encode()
    raw = encode(body, encoding)
    headers = {
        "Content-Type": "application/json", "Cache-Control": "no-store",
        "X-Request-Id": "failed-request", "Retry-After": "9",
        "Content-Length": str(len(raw)), "Transfer-Encoding": "chunked",
    }
    if encoding:
        headers["Content-Encoding"] = encoding
    relay, stream, _, responses, _ = make_relay(
        tmp_path, [raw[:1], raw[1:2], raw[2:]], status=status, headers=headers,
    )

    async def scenario():
        actual_status, forwarded, output = await forward(relay)
        assert actual_status == status
        assert b"".join([chunk async for chunk in output]) == body
        assert forwarded == {
            "content-type": "application/json", "cache-control": "no-store",
            "x-request-id": "failed-request", "retry-after": "9",
        }
        assert responses[0].is_closed and stream.close_count == 1
        await relay.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("interrupt", ["close", "cancel", "network_error"])
@pytest.mark.parametrize("encoding", [None, "gzip"])
def test_early_close_cancellation_and_upstream_error_close_response(
    tmp_path: Path, encoding, interrupt,
):
    chunks = gzip_events()[:3] if encoding else [FIRST_EVENT]
    failure = httpx.ReadError("private-stream-error") if interrupt == "network_error" else None
    if interrupt != "network_error":
        chunks.append(b"more-upstream-data")
    relay, stream, _, responses, _ = make_relay(
        tmp_path, chunks, headers={"Content-Encoding": "gzip"} if encoding else None,
        failure=failure, block_after=len(chunks) - 1 if interrupt == "cancel" else None,
    )

    async def scenario():
        _, _, output = await forward(relay)
        assert await anext(output) == FIRST_EVENT
        if interrupt == "network_error":
            with pytest.raises(httpx.ReadError):
                await anext(output)
        elif interrupt == "cancel":
            pending = asyncio.create_task(anext(output))
            await asyncio.wait_for(stream.waiting.wait(), timeout=1)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        else:
            await output.aclose()
        assert responses[0].is_closed and stream.close_count == 1
        await relay.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("corruption", ["gzip_header", "gzip_crc", "deflate"])
def test_corrupt_compression_raises_decoding_error_and_closes_response(
    tmp_path: Path, caplog, corruption,
):
    caplog.set_level(logging.DEBUG, logger="app.services.relay")
    encoding = "deflate" if corruption == "deflate" else "gzip"
    raw = bytearray(encode(SSE, encoding))
    raw[-8 if corruption == "gzip_crc" else 2] ^= 0xFF
    relay, stream, _, responses, _ = make_relay(
        tmp_path, [bytes(raw)], headers={"Content-Encoding": encoding},
    )

    async def scenario():
        _, _, output = await forward(relay)
        with pytest.raises(httpx.DecodingError):
            await anext(output)
        assert responses[0].is_closed and stream.close_count == 1
        await relay.aclose()

    asyncio.run(scenario())
    assert PROVIDER_SECRET not in caplog.text and "private-prompt" not in caplog.text


def test_late_gzip_corruption_propagates_without_synthesizing_done(tmp_path: Path):
    chunks = gzip_events()
    broken = bytearray(chunks[-1])
    broken[-8] ^= 0xFF
    chunks[-1] = bytes(broken)
    relay, stream, _, responses, _ = make_relay(
        tmp_path, chunks, headers={"Content-Encoding": "gzip"},
    )

    async def scenario():
        _, _, output = await forward(relay)
        assert await anext(output) == FIRST_EVENT
        assert await anext(output) == SECOND_EVENT
        with pytest.raises(httpx.DecodingError):
            await anext(output)
        assert responses[0].is_closed and stream.close_count == 1
        await relay.aclose()

    asyncio.run(scenario())


def test_streaming_response_client_disconnect_closes_upstream(tmp_path: Path):
    relay, stream, _, responses, _ = make_relay(
        tmp_path, gzip_events(), headers={"Content-Encoding": "gzip"},
        block_after=3,
    )

    async def scenario():
        status, headers, output = await forward(relay)
        first_sent = asyncio.Event()

        async def receive():
            await first_sent.wait()
            await stream.waiting.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body":
                assert message["body"] == FIRST_EVENT
                first_sent.set()

        response = StreamingResponse(output, status_code=status, headers=headers)
        # Uvicorn's current HTTP scope uses ASGI 2.3: disconnect cancels the
        # sender while it awaits the next upstream chunk.
        await asyncio.wait_for(response(
            {"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send,
        ), timeout=2)
        assert first_sent.is_set() and not stream.exhausted
        assert responses[0].is_closed and stream.close_count == 1
        await relay.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [200, 429, 500])
def test_fastapi_streaming_response_sends_decoded_body_with_consistent_headers(
    tmp_path: Path, status,
):
    body = SSE if status == 200 else b'{"error":{"message":"failed"}}'
    content_type = "text/event-stream" if status == 200 else "application/json"
    raw = encode(body, "gzip")
    _, stream, requests, responses, upstream = make_relay(
        tmp_path, [raw[:1], raw[1:2], raw[2:]], status=status,
        headers={
            "Content-Type": content_type, "Content-Encoding": "gzip",
            "Content-Length": str(len(raw)), "Transfer-Encoding": "chunked",
            "Cache-Control": "no-store", "Retry-After": "7",
        },
    )

    class NoopLauncher:
        async def launch(self, *args, **kwargs):
            raise AssertionError("No sandbox should be launched")

    app = create_app(
        ServerSettings(data_dir=tmp_path, provider_api_key=PROVIDER_SECRET),
        launcher=NoopLauncher(),
    )
    app.state.services.relay._client_factory = lambda: upstream
    with TestClient(app) as downstream:
        token = app.state.services.relay.token_registry.issue("session-a", 1)
        response = downstream.post(
            "/web/api/relay/v1/chat/completions", content=PROMPT,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        assert response.status_code == status and response.content == body
        assert response.headers["content-type"] == content_type
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["retry-after"] == "7"
        for name in ("content-encoding", "content-length", "transfer-encoding"):
            assert name not in response.headers
    assert len(requests) == 1 and requests[0].content == PROMPT
    assert responses[0].is_closed and stream.close_count == 1 and upstream.is_closed
