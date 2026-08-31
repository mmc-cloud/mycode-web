from collections.abc import AsyncIterator
import secrets
from typing import Callable

import httpx

from app.config import ServerSettings


class RelayAuthenticationError(PermissionError):
    pass


class RelayConfigurationError(RuntimeError):
    pass


class LLMRelay:
    def __init__(
        self,
        settings: ServerSettings,
        *,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self.settings = settings
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

    def authenticate(self, authorization: str | None) -> None:
        expected = f"Bearer {self.settings.relay_token}"
        if authorization is None or not secrets.compare_digest(
            authorization, expected
        ):
            raise RelayAuthenticationError("Invalid relay token.")

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
