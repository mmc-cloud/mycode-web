from collections.abc import AsyncIterator
from dataclasses import dataclass
import secrets
from threading import RLock
from typing import Callable

import httpx

from app.config import ServerSettings


class RelayAuthenticationError(PermissionError):
    pass


class RelayConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeTokenRecord:
    token: str
    session_id: str
    generation: int
    active: bool = True


class RuntimeTokenRegistry:
    """Small process-local registry for active Runtime relay credentials."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._tokens: dict[str, RuntimeTokenRecord] = {}
        self._session_tokens: dict[str, str] = {}

    def issue(self, session_id: str, generation: int) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            old_token = self._session_tokens.get(session_id)
            if old_token is not None:
                self._tokens.pop(old_token, None)
            self._tokens[token] = RuntimeTokenRecord(
                token=token,
                session_id=session_id,
                generation=generation,
            )
            self._session_tokens[session_id] = token
        return token

    def lookup(self, token: str) -> RuntimeTokenRecord | None:
        with self._lock:
            record = self._tokens.get(token)
            return record if record is not None and record.active else None

    def revoke(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock:
            record = self._tokens.pop(token, None)
            if record is None:
                return False
            if self._session_tokens.get(record.session_id) == token:
                self._session_tokens.pop(record.session_id, None)
            return True

    def revoke_session(self, session_id: str, generation: int | None = None) -> bool:
        with self._lock:
            token = self._session_tokens.get(session_id)
            if token is None:
                return False
            record = self._tokens.get(token)
            if record is None or (
                generation is not None and record.generation != generation
            ):
                return False
            self._tokens.pop(token, None)
            self._session_tokens.pop(session_id, None)
            return True

    def clear(self) -> None:
        with self._lock:
            self._tokens.clear()
            self._session_tokens.clear()

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._tokens)


class LLMRelay:
    def __init__(
        self,
        settings: ServerSettings,
        *,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        token_registry: RuntimeTokenRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.token_registry = token_registry or RuntimeTokenRegistry()
        self._client_factory = client_factory or (
            lambda: httpx.AsyncClient(timeout=None)
        )
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        if self._client is None:
            self._client = self._client_factory()

    async def aclose(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()

    def authenticate(self, authorization: str | None) -> RuntimeTokenRecord:
        if authorization is None or not authorization.startswith("Bearer "):
            raise RelayAuthenticationError("Invalid relay token.")
        token = authorization.removeprefix("Bearer ")
        record = self.token_registry.lookup(token)
        if record is None:
            raise RelayAuthenticationError("Invalid relay token.")
        return record

    async def forward(
        self, path: str, body: bytes, content_type: str | None
    ) -> tuple[int, dict[str, str], AsyncIterator[bytes]]:
        if not self.settings.provider_api_key:
            raise RelayConfigurationError(
                "MYCODE_PROVIDER_API_KEY is not configured on the server."
            )
        client = self._client
        if client is None:
            raise RelayConfigurationError("LLM relay is not started.")
        url = f"{self.settings.provider_base_url}/{path.lstrip('/')}"
        headers = {
            "Authorization": f"Bearer {self.settings.provider_api_key}",
            "Content-Type": content_type or "application/json",
            "Accept": "text/event-stream, application/json",
        }
        request = client.build_request("POST", url, headers=headers, content=body)
        response = await client.send(request, stream=True)

        response_headers = {}
        for name in ("content-type", "cache-control", "x-request-id"):
            value = response.headers.get(name)
            if value:
                response_headers[name] = value

        async def chunks() -> AsyncIterator[bytes]:
            try:
                async for chunk in response.aiter_raw():
                    yield chunk
            finally:
                await response.aclose()

        return response.status_code, response_headers, chunks()
