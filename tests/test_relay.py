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
    def __init__(
        self,
        content: bytes,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.content = content
        self.status_code = status_code
        self.headers = headers if headers is not None else {
            "content-type": "text/event-stream"
        }
        self.closed = False

    async def aiter_raw(self):
        yield self.content

    async def aclose(self) -> None:
        self.closed = True


class FakeClient:
    def __init__(self) -> None:
        self.requests: list[object] = []
        self.responses: list[FakeResponse] = []
        self.response_headers: list[dict[str, str]] = []
        self.response_statuses: list[int] = []
        self.closed = False

    def build_request(self, method, url, **kwargs):
        request = (method, url, kwargs)
        self.requests.append(request)
        return request

    async def send(self, request, *, stream):
        assert stream is True
        index = len(self.responses)
        response = FakeResponse(
            f"response-{index}".encode(),
            status_code=(
                self.response_statuses[index]
                if index < len(self.response_statuses)
                else 200
            ),
            headers=(
                self.response_headers[index]
                if index < len(self.response_headers)
                else None
            ),
        )
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
            await relay.forward(
                "chat/completions",
                b"{}",
                "application/json",
                session_id="session-a",
            )

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
                "chat/completions",
                b"{}",
                "application/json",
                session_id="session-a",
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


def test_relay_preserves_retry_after_and_only_allowlisted_response_headers(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fake_client = FakeClient()
        fake_client.response_statuses = [429]
        fake_client.response_headers = [
            {
                "content-type": "application/json",
                "cache-control": "no-store",
                "x-request-id": "request-123",
                "retry-after": "7",
                "set-cookie": "provider-secret=should-not-leak",
            }
        ]
        relay = LLMRelay(
            ServerSettings(data_dir=tmp_path, provider_api_key="provider-secret"),
            client_factory=lambda: fake_client,
        )
        await relay.start()
        status, headers, chunks = await relay.forward(
            "chat/completions",
            b"{}",
            "application/json",
            session_id="session-a",
        )
        assert status == 429
        assert headers == {
            "content-type": "application/json",
            "cache-control": "no-store",
            "x-request-id": "request-123",
            "retry-after": "7",
        }
        assert [chunk async for chunk in chunks] == [b"response-0"]
        await relay.aclose()

    import asyncio

    asyncio.run(scenario())


def test_relay_does_not_synthesize_missing_retry_after(tmp_path: Path) -> None:
    async def scenario() -> None:
        fake_client = FakeClient()
        fake_client.response_headers = [{"content-type": "application/json"}]
        relay = LLMRelay(
            ServerSettings(data_dir=tmp_path, provider_api_key="provider-secret"),
            client_factory=lambda: fake_client,
        )
        await relay.start()
        _, headers, chunks = await relay.forward(
            "chat/completions",
            b"{}",
            "application/json",
            session_id="session-a",
        )
        assert "retry-after" not in headers
        assert [chunk async for chunk in chunks] == [b"response-0"]
        await relay.aclose()

    import asyncio

    asyncio.run(scenario())


def test_relay_injects_opencode_session_from_authenticated_runtime_record(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fake_client = FakeClient()
        settings = ServerSettings(
            data_dir=tmp_path,
            provider_api_key="provider-secret",
            provider_base_url="https://opencode.ai/zen/go",
        )
        registry = RuntimeTokenRegistry()
        token = registry.issue("session-a", 1)
        relay = LLMRelay(
            settings,
            client_factory=lambda: fake_client,
            token_registry=registry,
        )
        await relay.start()
        record = relay.authenticate(f"Bearer {token}")
        _, _, chunks = await relay.forward(
            "chat/completions",
            b"{}",
            "application/json",
            session_id=record.session_id,
        )
        await chunks.__anext__()
        request_headers = fake_client.requests[0][2]["headers"]
        assert request_headers["User-Agent"] == "mycode-agent"
        assert request_headers["x-opencode-session"] == "session-a"
        await relay.aclose()

    import asyncio

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "provider_base_url",
    [
        "https://opencode.ai/zen/go/v1",
        "https://opencode.ai:443/zen/go/",
    ],
)
def test_relay_recognizes_supported_opencode_go_urls(
    tmp_path: Path, provider_base_url: str
) -> None:
    async def scenario() -> None:
        fake_client = FakeClient()
        relay = LLMRelay(
            ServerSettings(
                data_dir=tmp_path,
                provider_api_key="provider-secret",
                provider_base_url=provider_base_url,
            ),
            client_factory=lambda: fake_client,
        )
        await relay.start()
        _, _, chunks = await relay.forward(
            "chat/completions",
            b"{}",
            "application/json",
            session_id="session-a",
        )
        await chunks.__anext__()
        request_headers = fake_client.requests[0][2]["headers"]
        assert request_headers["User-Agent"] == "mycode-agent"
        assert request_headers["x-opencode-session"] == "session-a"
        await relay.aclose()

    import asyncio

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "provider_base_url",
    [
        "https://api.example.com/v1",
        "https://not-opencode.ai/zen/go",
        "http://opencode.ai/zen/go",
        "https://opencode.ai/zen/go?x=1",
        "https://user:pass@opencode.ai/zen/go",
        "https://opencode.ai/zen/chat/completions",
    ],
)
def test_relay_does_not_inject_opencode_headers_for_other_providers(
    tmp_path: Path, provider_base_url: str
) -> None:
    async def scenario() -> None:
        fake_client = FakeClient()
        relay = LLMRelay(
            ServerSettings(
                data_dir=tmp_path,
                provider_api_key="provider-secret",
                provider_base_url=provider_base_url,
            ),
            client_factory=lambda: fake_client,
        )
        await relay.start()
        _, _, chunks = await relay.forward(
            "chat/completions",
            b"{}",
            "application/json",
            session_id="session-a",
        )
        await chunks.__anext__()
        request_headers = fake_client.requests[0][2]["headers"]
        assert "x-opencode-session" not in request_headers
        assert "User-Agent" not in request_headers
        await relay.aclose()

    import asyncio

    asyncio.run(scenario())


def test_relay_route_ignores_spoofed_opencode_session_header(tmp_path: Path) -> None:
    fake_client = FakeClient()
    settings = ServerSettings(
        data_dir=tmp_path,
        provider_api_key="provider-secret",
        provider_base_url="https://opencode.ai/zen/go",
    )
    app = create_app(settings, launcher=NoopLauncher())
    relay = app.state.services.relay
    relay._client_factory = lambda: fake_client
    registry = relay.token_registry

    with TestClient(app) as client:
        token = registry.issue("session-a", 1)
        response = client.post(
            "/web/api/relay/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {token}",
                "x-opencode-session": "attacker-controlled",
            },
            json={"model": "test"},
        )

    assert response.status_code == 200
    request_headers = fake_client.requests[0][2]["headers"]
    assert request_headers["x-opencode-session"] == "session-a"
    assert request_headers["x-opencode-session"] != "attacker-controlled"


def test_relay_route_keeps_opencode_session_stable_across_token_rotation(
    tmp_path: Path,
) -> None:
    fake_client = FakeClient()
    settings = ServerSettings(
        data_dir=tmp_path,
        provider_api_key="provider-secret",
        provider_base_url="https://opencode.ai/zen/go",
    )
    app = create_app(settings, launcher=NoopLauncher())
    relay = app.state.services.relay
    relay._client_factory = lambda: fake_client
    registry = relay.token_registry

    with TestClient(app) as client:
        token_a = registry.issue("session-a", 1)
        assert client.post(
            "/web/api/relay/v1/chat/completions",
            headers={"Authorization": f"Bearer {token_a}"},
            json={"model": "test"},
        ).status_code == 200
        token_b = registry.issue("session-a", 2)
        assert client.post(
            "/web/api/relay/v1/chat/completions",
            headers={"Authorization": f"Bearer {token_b}"},
            json={"model": "test"},
        ).status_code == 200

    request_headers = [request[2]["headers"] for request in fake_client.requests]
    assert [headers["x-opencode-session"] for headers in request_headers] == [
        "session-a",
        "session-a",
    ]


def test_relay_route_isolates_opencode_sessions(tmp_path: Path) -> None:
    fake_client = FakeClient()
    settings = ServerSettings(
        data_dir=tmp_path,
        provider_api_key="provider-secret",
        provider_base_url="https://opencode.ai/zen/go",
    )
    app = create_app(settings, launcher=NoopLauncher())
    relay = app.state.services.relay
    relay._client_factory = lambda: fake_client
    registry = relay.token_registry

    with TestClient(app) as client:
        for session_id in ("session-a", "session-b"):
            token = registry.issue(session_id, 1)
            assert client.post(
                "/web/api/relay/v1/chat/completions",
                headers={"Authorization": f"Bearer {token}"},
                json={"model": "test"},
            ).status_code == 200

    request_headers = [request[2]["headers"] for request in fake_client.requests]
    assert [headers["x-opencode-session"] for headers in request_headers] == [
        "session-a",
        "session-b",
    ]


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
