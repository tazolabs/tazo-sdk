"""The four customer-facing resources, shared by Tazo and AsyncTazo."""

from __future__ import annotations

import asyncio
from typing import Literal
from uuid import uuid4

from ._audio import AudioInput, snapshot_audio
from ._http import AsyncHTTPClient, HTTPClient
from .errors import InvalidRequestError, ProtocolError
from .streaming import (
    AsyncStreamingSession,
    StreamingSession,
    _State,
    decision_request,
)
from .types import (
    DecisionRequest,
    DecisionResponse,
    HealthStatus,
    ModelList,
    QuestionsInput,
    StructuredValue,
)


def _operation_key(value: str | None) -> str:
    if value is None:
        return str(uuid4())
    if (
        not value.strip()
        or len(value) > 256
        or not value.isascii()
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise InvalidRequestError("idempotency_key must be 1 to 256 printable ASCII characters.")
    return value.strip()


def _check_answers(result: DecisionResponse, request: DecisionRequest) -> DecisionResponse:
    if set(result.answers) != set(request.questions):
        raise ProtocolError(
            "Response question IDs do not match the request.",
            request_id=result.request_id,
            idempotency_key=result.idempotency_key,
        )
    for question_id, question in request.questions.items():
        answer = result.answers[question_id]
        if answer.type != question.type or (
            answer.type == "choice"
            and question.type == "choice"
            and set(answer.probabilities) != set(question.criteria)
        ):
            raise ProtocolError(
                "Response answers do not match the requested question.",
                request_id=result.request_id,
                idempotency_key=result.idempotency_key,
            )
    return result


def _stream_url(base_url: str) -> str:
    return (
        ("wss" if base_url.startswith("https:") else "ws")
        + base_url[base_url.index(":") :]
        + "/v1/decisions/stream"
    )


class Decisions:
    """Uploaded and streaming audio decisions."""

    def __init__(self, http: HTTPClient) -> None:
        self._http = http

    def create(
        self,
        *,
        audio: AudioInput,
        state: StructuredValue,
        questions: QuestionsInput,
        model: str = "tazo-large",
        idempotency_key: str | None = None,
        filename: str | None = None,
        content_type: str | None = None,
    ) -> DecisionResponse:
        """Upload audio and wait for a typed decision.

        Args:
            audio: Path, bytes, or a binary stream. Streams are read from their
                current position; seekable streams are restored and never closed.
            state: Shared context as a string, object, or list of strings.
            questions: Question-ID map of choice/noul dictionaries or typed models.
            model: Public model ID. Defaults to tazo-large.
            idempotency_key: Retained operation key, or None to generate a new one.
                Every automatic retry reuses the same key and upload snapshot.
            filename: Optional name for bytes or file objects.
            content_type: Optional MIME type; otherwise inferred from filename.

        Returns:
            Typed result, including request_id and idempotency_key SDK metadata.

        Raises:
            DecisionPendingError: Work continues after the retry budget is exhausted.
            TazoError: Authentication, validation, transport, or service failure.
        """
        request = decision_request(model, state, questions)
        key = _operation_key(idempotency_key)
        self._http.headers(authenticated=True, key=key)
        upload = snapshot_audio(audio, filename, content_type)
        result = self._http.request(
            "POST",
            "/v1/decisions",
            DecisionResponse,
            key=key,
            request_json=request.model_dump_json(exclude_none=True),
            upload=upload,
        )
        return _check_answers(result, request)

    def stream(
        self,
        *,
        state: StructuredValue,
        questions: QuestionsInput,
        model: str = "tazo-large",
        audio_scope: Literal["turn", "chunk"] = "turn",
        evaluation_interval_ms: int | None = None,
        event_timeout: float = 65,
    ) -> StreamingSession:
        """Create a context-managed session for one question and multiple turns.

        Args:
            state: Context for the decision.
            questions: Exactly one choice or noul question.
            model: Public model ID; the model's advertised capabilities apply.
            audio_scope: turn evaluates cumulative audio; chunk evaluates new audio.
            evaluation_interval_ms: Integer from 100 to 30000, or server default.
            event_timeout: Maximum seconds waiting for each server event.

        Returns:
            A StreamingSession; entering it authenticates and waits for readiness.
        """
        self._http.headers(authenticated=True, key=None)
        return StreamingSession(
            _stream_url(self._http.base_url),
            self._http.credential(),
            _State(
                decision_request(model, state, questions),
                audio_scope,
                evaluation_interval_ms,
                event_timeout,
            ),
        )


class AsyncDecisions:
    """Native asynchronous upload and WebSocket decisions."""

    def __init__(self, http: AsyncHTTPClient) -> None:
        self._http = http

    async def create(
        self,
        *,
        audio: AudioInput,
        state: StructuredValue,
        questions: QuestionsInput,
        model: str = "tazo-large",
        idempotency_key: str | None = None,
        filename: str | None = None,
        content_type: str | None = None,
    ) -> DecisionResponse:
        """Async counterpart of Decisions.create, with identical input and retry policy.

        File reads run in a worker thread; HTTP uses native asynchronous I/O.
        Cancellation does not imply cancellation of accepted server-side work.
        Retain a caller-supplied key when cancellation recovery is required.
        """
        request = decision_request(model, state, questions)
        key = _operation_key(idempotency_key)
        self._http.headers(authenticated=True, key=key)
        upload = await asyncio.to_thread(snapshot_audio, audio, filename, content_type)
        result = await self._http.request(
            "POST",
            "/v1/decisions",
            DecisionResponse,
            key=key,
            request_json=request.model_dump_json(exclude_none=True),
            upload=upload,
        )
        return _check_answers(result, request)

    def stream(
        self,
        *,
        state: StructuredValue,
        questions: QuestionsInput,
        model: str = "tazo-large",
        audio_scope: Literal["turn", "chunk"] = "turn",
        evaluation_interval_ms: int | None = None,
        event_timeout: float = 65,
    ) -> AsyncStreamingSession:
        """Create an async context manager; use async for to receive typed events.

        Parameters match Decisions.stream. This method isn't awaited; use
        ``async with client.decisions.stream(...) as session``.
        """
        self._http.headers(authenticated=True, key=None)
        return AsyncStreamingSession(
            _stream_url(self._http.base_url),
            self._http.credential(),
            _State(
                decision_request(model, state, questions),
                audio_scope,
                evaluation_interval_ms,
                event_timeout,
            ),
        )


class Models:
    """Discover public models and their capabilities."""

    def __init__(self, http: HTTPClient) -> None:
        self._http = http

    def list(self) -> ModelList:
        """Return current models, capabilities, and readiness."""
        return self._http.request("GET", "/v1/models", ModelList)


class AsyncModels:
    """Asynchronous model discovery."""

    def __init__(self, http: AsyncHTTPClient) -> None:
        self._http = http

    async def list(self) -> ModelList:
        """Return current models, capabilities, and readiness."""
        return await self._http.request("GET", "/v1/models", ModelList)


class Health:
    """Public, unauthenticated model readiness."""

    def __init__(self, http: HTTPClient) -> None:
        self._http = http

    def check(self) -> HealthStatus:
        """Return ok or unavailable; the documented HTTP 503 is a health result."""
        return self._http.request("GET", "/healthz", HealthStatus, authenticated=False, health=True)


class AsyncHealth:
    """Asynchronous, unauthenticated readiness."""

    def __init__(self, http: AsyncHTTPClient) -> None:
        self._http = http

    async def check(self) -> HealthStatus:
        """Return ok or unavailable, without sending a bearer credential."""
        return await self._http.request(
            "GET", "/healthz", HealthStatus, authenticated=False, health=True
        )
