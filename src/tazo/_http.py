"""Shared request policy with native sync and async HTTP adapters."""

from __future__ import annotations

import asyncio
import builtins
import math
import os
import random
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TypeVar
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ValidationError

from ._audio import Upload
from ._version import __version__
from .errors import (
    AuthenticationError,
    ConnectionError,
    DecisionPendingError,
    IdempotencyConflictError,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
    ProtocolError,
    RateLimitError,
    RequestTooLargeError,
    ServiceError,
    TazoError,
    TimeoutError,
)
from .types import DecisionResponse

ResponseT = TypeVar("ResponseT", bound=BaseModel)
DEFAULT_TIMEOUT = httpx.Timeout(connect=5, read=65, write=30, pool=5)


def retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        delay = float(value)
    except ValueError:
        try:
            instant = parsedate_to_datetime(value)
            if instant.tzinfo is None:
                instant = instant.replace(tzinfo=UTC)
            delay = (instant - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0, delay) if math.isfinite(delay) else None


def response_error(response: httpx.Response, key: str | None) -> TazoError:
    code = None
    message = f"Tazo returned HTTP {response.status_code}."
    retryable = response.status_code in {408, 429, 500, 502, 503, 504}
    request_id = response.headers.get("X-Tazo-Request-Id")
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        request_id = request_id or (
            body.get("request_id") if isinstance(body.get("request_id"), str) else None
        )
        error = body.get("error")
        if isinstance(error, dict):
            code = error.get("code") if isinstance(error.get("code"), str) else None
            if isinstance(error.get("message"), str):
                message = error["message"]
            if isinstance(error.get("retryable"), bool):
                retryable = error["retryable"]
    cls: type[TazoError] = {
        400: InvalidRequestError,
        401: AuthenticationError,
        403: PermissionDeniedError,
        404: NotFoundError,
        409: IdempotencyConflictError,
        413: RequestTooLargeError,
        422: InvalidRequestError,
        429: RateLimitError,
    }.get(response.status_code, ServiceError)
    # Authentication and invalid input must never cause retry loops, even if a
    # malformed server response sets retryable=true.
    if response.status_code in {400, 401, 403, 404, 409, 413, 422}:
        retryable = False
    if code == "decision_pending":
        cls = DecisionPendingError
    return cls(
        message,
        status_code=response.status_code,
        code=code,
        request_id=request_id,
        idempotency_key=key,
        retryable=retryable,
        retry_after=retry_after(response.headers.get("Retry-After")),
    )


class HTTPPolicy:
    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str,
        timeout: float | httpx.Timeout,
        max_retries: int,
        operation_timeout: float,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "base_url must be an HTTP(S) URL without credentials, query, or fragment."
            )
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
            raise ValueError("max_retries must be a nonnegative integer.")
        if not math.isfinite(operation_timeout) or operation_timeout <= 0:
            raise ValueError("operation_timeout must be positive and finite.")
        self.base_url = parsed.geturl().rstrip("/")
        self._api_key = api_key if api_key is not None else os.getenv("TAZO_API_KEY")
        if self._api_key is not None and (
            not self._api_key.strip()
            or not self._api_key.isascii()
            or any(ord(c) < 32 or ord(c) == 127 for c in self._api_key)
        ):
            raise ValueError("api_key must contain printable ASCII characters.")
        self.timeout = timeout if isinstance(timeout, httpx.Timeout) else httpx.Timeout(timeout)
        self.max_retries = max_retries
        self.operation_timeout = operation_timeout
        self.closed = False

    def credential(self) -> str:
        if not self._api_key:
            raise AuthenticationError("Set TAZO_API_KEY or pass api_key to the client.")
        return self._api_key

    def headers(self, *, authenticated: bool, key: str | None) -> dict[str, str]:
        if self.closed:
            raise TazoError("This client is closed.")
        headers = {"User-Agent": f"tazo-python/{__version__}", "Accept": "application/json"}
        if authenticated:
            headers["Authorization"] = f"Bearer {self.credential()}"
        if key is not None:
            headers["Idempotency-Key"] = key
        return headers

    def attempt_timeout(self, remaining: float) -> httpx.Timeout:
        def cap(value: float | None) -> float:
            return remaining if value is None else min(value, remaining)

        return httpx.Timeout(
            **{
                name: cap(getattr(self.timeout, name))
                for name in ("connect", "read", "write", "pool")
            }
        )

    def delay(self, error: TazoError, attempt: int) -> float:
        if error.retry_after is not None:
            return error.retry_after
        return random.uniform(0, min(8, 0.5 * 2 ** min(attempt, 8)))

    @staticmethod
    def decode(response: httpx.Response, model: type[ResponseT], key: str | None) -> ResponseT:
        try:
            result = model.model_validate_json(response.content)
        except ValidationError:
            raise ProtocolError(
                "Tazo returned an invalid response.",
                status_code=response.status_code,
                request_id=response.headers.get("X-Tazo-Request-Id"),
                idempotency_key=key,
            ) from None
        if isinstance(result, DecisionResponse):
            result.request_id = response.headers.get("X-Tazo-Request-Id")
            result.idempotency_key = key
        return result

    @staticmethod
    def deadline_error(key: str | None, last: TazoError | None) -> TazoError:
        if last is not None:
            return last
        return TimeoutError(
            "The operation deadline was reached; accepted decision work may continue.",
            idempotency_key=key,
            retryable=True,
        )


class HTTPClient(HTTPPolicy):
    def __init__(self, *, http_client: httpx.Client | None = None, **kwargs: object) -> None:
        # Public constructors provide the typed policy arguments explicitly.
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._client = http_client or httpx.Client()
        self._owns_client = http_client is None

    def close(self) -> None:
        if not self.closed and self._owns_client:
            self._client.close()
        self.closed = True

    def request(
        self,
        method: str,
        path: str,
        model: type[ResponseT],
        *,
        authenticated: bool = True,
        key: str | None = None,
        request_json: str | None = None,
        upload: Upload | None = None,
        health: bool = False,
    ) -> ResponseT:
        headers = self.headers(authenticated=authenticated, key=key)
        deadline = time.monotonic() + self.operation_timeout
        last: TazoError | None = None
        for attempt in range(self.max_retries + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise self.deadline_error(key, last)
            try:
                response = self._client.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=headers,
                    timeout=self.attempt_timeout(remaining),
                    follow_redirects=False,
                    files=None
                    if upload is None
                    else {
                        "request": (None, request_json or "", "application/json"),
                        "audio": (upload.filename, upload.data, upload.content_type),
                    },
                )
            except httpx.TimeoutException:
                last = TimeoutError("Tazo request timed out.", idempotency_key=key, retryable=True)
            except httpx.TransportError:
                last = ConnectionError(
                    "Could not connect to Tazo.", idempotency_key=key, retryable=True
                )
            else:
                if response.status_code == 200 or (health and response.status_code == 503):
                    return self.decode(response, model, key)
                last = response_error(response, key)
            if not last.retryable or attempt == self.max_retries:
                raise last
            delay = self.delay(last, attempt)
            if delay >= deadline - time.monotonic():
                raise last
            time.sleep(delay)
        raise AssertionError("Unreachable retry state")


class AsyncHTTPClient(HTTPPolicy):
    def __init__(self, *, http_client: httpx.AsyncClient | None = None, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._client = http_client or httpx.AsyncClient()
        self._owns_client = http_client is None

    async def close(self) -> None:
        if not self.closed and self._owns_client:
            await self._client.aclose()
        self.closed = True

    async def request(
        self,
        method: str,
        path: str,
        model: type[ResponseT],
        *,
        authenticated: bool = True,
        key: str | None = None,
        request_json: str | None = None,
        upload: Upload | None = None,
        health: bool = False,
    ) -> ResponseT:
        headers = self.headers(authenticated=authenticated, key=key)
        deadline = time.monotonic() + self.operation_timeout
        last: TazoError | None = None
        for attempt in range(self.max_retries + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise self.deadline_error(key, last)
            try:
                async with asyncio.timeout(remaining):
                    response = await self._client.request(
                        method,
                        f"{self.base_url}{path}",
                        headers=headers,
                        timeout=self.attempt_timeout(remaining),
                        follow_redirects=False,
                        files=None
                        if upload is None
                        else {
                            "request": (None, request_json or "", "application/json"),
                            "audio": (upload.filename, upload.data, upload.content_type),
                        },
                    )
            except (httpx.TimeoutException, builtins.TimeoutError):
                last = TimeoutError("Tazo request timed out.", idempotency_key=key, retryable=True)
            except httpx.TransportError:
                last = ConnectionError(
                    "Could not connect to Tazo.", idempotency_key=key, retryable=True
                )
            else:
                if response.status_code == 200 or (health and response.status_code == 503):
                    return self.decode(response, model, key)
                last = response_error(response, key)
            if not last.retryable or attempt == self.max_retries:
                raise last
            delay = self.delay(last, attempt)
            if delay >= deadline - time.monotonic():
                raise last
            await asyncio.sleep(delay)
        raise AssertionError("Unreachable retry state")
