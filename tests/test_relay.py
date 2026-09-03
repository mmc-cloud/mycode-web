from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import ServerSettings
from app.main import create_app
from app.services.relay import (
    LLMRelay,
    RelayAuthenticationError,
    RelayConfigurationError,
    RuntimeTokenRegistry,
)


class NoopLauncher:
    async def launch(self, session_id, workspace, mycode_state):
        raise AssertionError("Sandbox launch is not expected in relay tests.")


class FakeResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.status_code = 200
        self.headers = {"content-type": "text/event-stream"}
        self.closed = False

    async def aiter_raw(self):
        yield self.content

    async def aclose(self) -> None:
        self.closed = True


class FakeClient:
    def __init__(self) -> None:
        self.requests: list[object] = []
        self.responses: list[FakeResponse] = []
        self.closed = False

    def build_request(self, method, url, **kwargs):
        request = (method, url, kwargs)
        self.requests.append(request)
        return request

    async def send(self, request, *, stream):
        assert stream is True
        response = FakeResponse(f"response-{len(self.responses)}".encode())
        self.responses.append(response)
        return response

    async def aclose(self) -> None:
        self.closed = True


def test_relay_token_validation_and_missing_provider_configuration(tmp_path: Path) -> None:
    settings = ServerSettings(
        data_dir=tmp_path,
        provider_api_key=None,
    )
    registry = RuntimeTokenRegistry()
    token = registry.issue("session-a", 1)
    relay = LLMRelay(settings, token_registry=registry)
    with pytest.raises(RelayAuthenticationError):
        relay.authenticate("Bearer wrong")
    relay.authenticate(f"Bearer {token}")

    async def missing_provider() -> None:
        with pytest.raises(RelayConfigurationError):
            await relay.forward("chat/completions", b"{}", "application/json")

    import asyncio

    asyncio.run(missing_provider())


def test_runtime_tokens_are_random_scoped_and_revocable() -> None:
    registry = RuntimeTokenRegistry()
    token_a = registry.issue("session-a", 1)
    token_b = registry.issue("session-b", 1)
    relay = LLMRelay(
        ServerSettings(),
        token_registry=registry,
    )

    assert token_a != token_b
    assert len(token_a) >= 43
    assert relay.authenticate(f"Bearer {token_a}").session_id == "session-a"
    assert relay.authenticate(f"Bearer {token_b}").session_id == "session-b"
    with pytest.raises(RelayAuthenticationError):
        relay.authenticate("Bearer random-token")

    registry.revoke(token_a)
    with pytest.raises(RelayAuthenticationError):
        relay.authenticate(f"Bearer {token_a}")

    token_a_restart = registry.issue("session-a", 2)
    assert token_a_restart != token_a
    assert registry.lookup(token_a) is None
    assert relay.authenticate(f"Bearer {token_b}").session_id == "session-b"


def test_relay_api_rejects_missing_internal_token(tmp_path: Path) -> None:
    settings = ServerSettings(
        data_dir=tmp_path,
        provider_api_key="provider-secret",
    )
    with TestClient(create_app(settings, launcher=NoopLauncher())) as client:
        response = client.post(
            "/web/api/relay/v1/chat/completions", json={"model": "test"}
        )
    assert response.status_code == 401
    assert "provider-secret" not in response.text
    assert "set-cookie" not in response.headers


def test_relay_reuses_lifespan_client_and_closes_it(tmp_path: Path) -> None:
    async def scenario() -> None:
        settings = ServerSettings(
            data_dir=tmp_path,
            provider_api_key="provider-secret",
        )
        fake_client = FakeClient()
        factory_calls = 0

        def factory():
            nonlocal factory_calls
            factory_calls += 1
            return fake_client

        relay = LLMRelay(settings, client_factory=factory)
        await relay.start()
        for _ in range(2):
            _, _, chunks = await relay.forward(
                "chat/completions", b"{}", "application/json"
            )
            assert [chunk async for chunk in chunks]
        assert factory_calls == 1
        assert len(fake_client.requests) == 2
        assert all(response.closed for response in fake_client.responses)
        assert fake_client.closed is False
        await relay.aclose()
        assert fake_client.closed is True

    import asyncio

    asyncio.run(scenario())


def test_app_lifespan_starts_and_stops_relay_client(tmp_path: Path) -> None:
    app = create_app(
        ServerSettings(
            data_dir=tmp_path,
            provider_api_key=None,
        ),
        launcher=NoopLauncher(),
    )
    relay = app.state.services.relay
    assert relay._client is None
    with TestClient(app):
        assert relay._client is not None
    assert relay._client is None
