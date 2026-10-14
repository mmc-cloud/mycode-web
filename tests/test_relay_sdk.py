"""Optional OpenAI SDK protocol regression; all transports are local mocks.

Also runnable in an existing SDK environment with httpx and pytest installed.
No package installation, Provider credential or external request is required.
"""
import asyncio
import gzip

import httpx
import pytest

from app.config import ServerSettings
from app.services.relay import LLMRelay


def test_openai_sdk_parses_provider_gzip_sse_after_relay_decoding(tmp_path):
    openai = pytest.importorskip("openai", reason="OpenAI SDK is not a Web backend dependency")
    sse = (
        b'data: {"id":"test","object":"chat.completion.chunk","created":0,"model":"test",'
        b'"choices":[{"index":0,"delta":{"role":"assistant","content":"Hello"},"finish_reason":null}]}\n\n'
        b'data: {"id":"test","object":"chat.completion.chunk","created":0,"model":"test",'
        b'"choices":[{"index":0,"delta":{"content":" World"},"finish_reason":"stop"}]}\n\n'
        b'data: [DONE]\n\n'
    )
    raw = gzip.compress(sse, mtime=0)
    assert raw[:2] == b"\x1f\x8b"

    class ProviderStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield raw[:1]
            yield raw[1:2]
            yield raw[2:]

        async def aclose(self):
            self.closed = True

    class RelayStream(httpx.AsyncByteStream):
        def __init__(self, chunks):
            self.chunks = chunks

        async def __aiter__(self):
            async for chunk in self.chunks:
                yield chunk

        async def aclose(self):
            await self.chunks.aclose()

    async def scenario():
        requests = []
        provider_stream = ProviderStream()

        def provider(request):
            requests.append(request)
            return httpx.Response(
                200, headers={"Content-Type": "text/event-stream", "Content-Encoding": "gzip"},
                stream=provider_stream,
            )

        upstream = httpx.AsyncClient(transport=httpx.MockTransport(provider), trust_env=False)
        relay = LLMRelay(
            ServerSettings(
                data_dir=tmp_path, provider_api_key="mock-provider-key",
                provider_base_url="https://provider.invalid/v1",
            ), client_factory=lambda: upstream,
        )
        await relay.start()
        token = relay.token_registry.issue("session-a", 1)

        async def downstream(request):
            record = relay.authenticate(request.headers.get("authorization"))
            status, headers, chunks = await relay.forward(
                "chat/completions", request.content, request.headers.get("content-type"),
                session_id=record.session_id,
            )
            assert "content-encoding" not in headers and "content-length" not in headers
            return httpx.Response(status, headers=headers, stream=RelayStream(chunks))

        client = openai.AsyncOpenAI(
            api_key=token, base_url="https://relay.invalid/v1", max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(downstream), trust_env=False),
        )
        try:
            stream = await client.chat.completions.create(
                model="test", messages=[{"role": "user", "content": "synthetic prompt"}], stream=True,
            )
            async with stream:
                deltas = [chunk.choices[0].delta.content async for chunk in stream]
            assert deltas == ["Hello", " World"]
            assert len(requests) == 1
            assert requests[0].headers["authorization"] == "Bearer mock-provider-key"
            assert provider_stream.closed
        finally:
            await client.close()
            await relay.aclose()
        assert upstream.is_closed

    asyncio.run(scenario())
