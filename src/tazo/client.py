"""Public client configuration and connection ownership."""

from __future__ import annotations

import httpx

from ._http import DEFAULT_TIMEOUT, AsyncHTTPClient, HTTPClient
from .resources import AsyncDecisions, AsyncHealth, AsyncModels, Decisions, Health, Models


class Tazo:
    """Synchronous client for the four public Tazo operations.

    Args:
        api_key: Bearer credential, or read TAZO_API_KEY. May be omitted for health.
        base_url: API origin, optionally with a proxy path prefix; no /v1 suffix.
        timeout: Per-network-phase timeout in seconds, or an httpx.Timeout.
        max_retries: Additional HTTP attempts after the first (default two).
        operation_timeout: HTTP retry budget in seconds (default 180). Each network
            phase is capped by the remaining budget. A synchronous in-flight request
            can outlast this budget across multiple network phases/body chunks.
        http_client: Optional caller-owned HTTPX client; the SDK will not close it.

    Use as a context manager, or call close. Streaming sockets have their own
    context managers and must be closed before closing this client.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.tazolabs.com",
        timeout: float | httpx.Timeout = DEFAULT_TIMEOUT,
        max_retries: int = 2,
        operation_timeout: float = 180,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._http = HTTPClient(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            operation_timeout=operation_timeout,
            http_client=http_client,
        )
        self.decisions = Decisions(self._http)
        self.models = Models(self._http)
        self.health = Health(self._http)

    def close(self) -> None:
        """Release the SDK-owned HTTP pool. Safe to call more than once."""
        self._http.close()

    def __enter__(self) -> Tazo:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class AsyncTazo:
    """Native async client with the same configuration as Tazo.

    Await create, list, and check. Use async with for streaming sessions.
    operation_timeout also enforces an async deadline during each HTTP attempt.
    Supplied HTTPX clients remain caller-owned.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.tazolabs.com",
        timeout: float | httpx.Timeout = DEFAULT_TIMEOUT,
        max_retries: int = 2,
        operation_timeout: float = 180,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._http = AsyncHTTPClient(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            operation_timeout=operation_timeout,
            http_client=http_client,
        )
        self.decisions = AsyncDecisions(self._http)
        self.models = AsyncModels(self._http)
        self.health = AsyncHealth(self._http)

    async def close(self) -> None:
        """Release the SDK-owned HTTP pool."""
        await self._http.close()

    async def __aenter__(self) -> AsyncTazo:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
