"""Managed streaming sessions. No reconnect, replay, or unbounded application queues."""

from __future__ import annotations

import asyncio
import json
import math
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import AbstractContextManager
from typing import Literal

from pydantic import ValidationError
from websockets.asyncio.client import ClientConnection as AsyncConnection
from websockets.asyncio.client import connect as async_connect
from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.sync.client import ClientConnection as SyncConnection
from websockets.sync.client import connect as sync_connect

from ._version import __version__
from .errors import InvalidRequestError, ProtocolError, StreamingError, TazoError
from .types import (
    DecisionEvent,
    DecisionRequest,
    ErrorEvent,
    QuestionsInput,
    SessionClosed,
    SessionReady,
    SessionUpdated,
    StreamEvent,
    StructuredValue,
    UnknownEvent,
)


def decision_request(
    model: str, state: StructuredValue, questions: QuestionsInput
) -> DecisionRequest:
    """Validate input without leaking submitted values through validation exceptions."""
    try:
        return DecisionRequest.model_validate(
            {"model": model, "state": state, "questions": questions}
        )
    except ValidationError:
        raise InvalidRequestError(
            "Provide model, state, and nonblank question IDs with valid choice or noul questions."
        ) from None


class _State:
    def __init__(
        self,
        request: DecisionRequest,
        audio_scope: Literal["turn", "chunk"],
        evaluation_interval_ms: int | None,
        event_timeout: float,
        sample_rate_hz: int = 16000,
    ) -> None:
        if (
            isinstance(sample_rate_hz, bool)
            or not isinstance(sample_rate_hz, int)
            or not 8000 <= sample_rate_hz <= 48000
        ):
            raise InvalidRequestError("sample_rate_hz must be an integer from 8000 to 48000.")
        if len(request.questions) != 1:
            raise InvalidRequestError("Streaming requires exactly one question.")
        if audio_scope not in {"turn", "chunk"}:
            raise InvalidRequestError("audio_scope must be turn or chunk.")
        if evaluation_interval_ms is not None and (
            isinstance(evaluation_interval_ms, bool)
            or not isinstance(evaluation_interval_ms, int)
            or not 100 <= evaluation_interval_ms <= 30000
        ):
            raise InvalidRequestError(
                "evaluation_interval_ms must be an integer from 100 to 30000."
            )
        if not math.isfinite(event_timeout) or event_timeout <= 0:
            raise ValueError("event_timeout must be positive and finite.")
        self.request = request
        self.sample_rate_hz = sample_rate_hz
        self.audio_scope = audio_scope
        self.interval = evaluation_interval_ms
        self.event_timeout = event_timeout
        self.phase = "new"
        self.ready: SessionReady | None = None
        self.audio_bytes = 0
        self.sequence = 0

    def start(self, credential: str) -> str:
        payload = {
            **self.request.model_dump(mode="json", exclude_none=True),
            "type": "session.start",
            "authorization": f"Bearer {credential}",
            "audio_scope": self.audio_scope,
            "input_audio": {
                "encoding": "pcm_s16le",
                "sample_rate_hz": self.sample_rate_hz,
                "channels": 1,
            },
        }
        if self.interval is not None:
            payload["evaluation_interval_ms"] = self.interval
        return json.dumps(payload)

    def initialize(self, raw: str | bytes) -> None:
        event = self.parse(raw)
        if not isinstance(event, SessionReady):
            raise ProtocolError("Expected session.ready before audio.")
        self.ready = event
        self.sequence = event.sequence
        self.phase = "idle"

    @staticmethod
    def parse(raw: str | bytes) -> SessionReady | StreamEvent:
        try:
            body = json.loads(raw)
            if not isinstance(body, dict) or not isinstance(body.get("type"), str):
                raise ValueError
            kind = body["type"]
            if kind == "session.ready":
                return SessionReady.model_validate(body)
            if kind == "session.updated":
                return SessionUpdated.model_validate(body)
            if kind in {"decision.updated", "decision.finalized"}:
                return DecisionEvent.model_validate(body)
            if kind == "session.closed":
                return SessionClosed.model_validate(body)
            if kind == "error":
                error = ErrorEvent.model_validate(body)
                if error.fatal:
                    raise StreamingError(error.error.message, code=error.error.code)
                return error
            return UnknownEvent.model_validate(body)
        except (ValueError, TypeError, UnicodeDecodeError):
            raise ProtocolError("Tazo returned an invalid streaming event.") from None

    def apply(self, raw: str | bytes) -> StreamEvent:
        event = self.parse(raw)
        if isinstance(event, SessionReady):
            raise ProtocolError("Received a second session.ready event.")
        if isinstance(event, (SessionUpdated, DecisionEvent, SessionClosed, ErrorEvent)):
            if event.sequence is not None:
                if event.sequence <= self.sequence:
                    raise ProtocolError("Streaming sequence numbers must increase.")
                self.sequence = event.sequence
            if event.session_id is not None and self.ready is not None:
                if event.session_id != self.ready.session_id:
                    raise ProtocolError("Received an event for another streaming session.")
        if isinstance(event, DecisionEvent) and event.type == "decision.finalized":
            self.audio_bytes = 0
            if self.phase != "finishing":
                self.phase = "idle"
        elif isinstance(event, SessionUpdated):
            self.phase = "idle"
        elif isinstance(event, SessionClosed):
            self.phase = "closed"
        elif isinstance(event, ErrorEvent) and self.phase == "updating":
            self.phase = "idle"
        return event

    def validate_audio(self, audio: bytes) -> None:
        if self.phase not in {"idle", "collecting"} or self.ready is None:
            raise InvalidRequestError("Wait for the session or turn acknowledgement before audio.")
        if not isinstance(audio, bytes) or not audio or len(audio) % 2:
            raise InvalidRequestError("Send nonempty, even-length PCM16 bytes without WAV headers.")
        if len(audio) > self.ready.max_audio_message_bytes:
            raise InvalidRequestError(
                "Audio message exceeds session.ready.max_audio_message_bytes."
            )
        if (
            self.ready.max_turn_audio_ms is not None
            and (self.audio_bytes + len(audio)) * 1000
            > self.ready.max_turn_audio_ms * self.sample_rate_hz * 2
        ):
            raise InvalidRequestError("Commit before exceeding session.ready.max_turn_audio_ms.")

    def control(
        self,
        kind: str,
        *,
        state: StructuredValue | None = None,
        questions: QuestionsInput | None = None,
    ) -> str:
        if kind == "input.commit":
            if self.phase != "collecting":
                raise InvalidRequestError(
                    "Send audio before commit; wait for finalized before another turn."
                )
            self.phase = "finalizing"
            return json.dumps({"type": kind})
        if kind == "input.finish":
            if self.phase not in {"idle", "collecting", "finalizing"}:
                raise InvalidRequestError("The session cannot finish in its current state.")
            self.phase = "finishing"
            return json.dumps({"type": kind})
        if self.phase != "idle" or state is None or questions is None:
            raise InvalidRequestError("Update both state and questions between turns.")
        request = decision_request(self.request.model, state, questions)
        if len(request.questions) != 1:
            raise InvalidRequestError("Streaming requires exactly one question.")
        self.phase = "updating"
        return json.dumps(
            {
                "type": kind,
                "state": request.state,
                "questions": request.model_dump(mode="json")["questions"],
            }
        )


class StreamingSession(Iterator[StreamEvent]):
    """Synchronous session; use as a context manager and iterate events in one thread.

    Send audio from another thread while iterating to receive provisional updates.
    ``commit`` and ``update`` send control messages; consume the corresponding
    acknowledgement before sending the next turn. ``finish`` requests a graceful
    end; keep iterating until ``session.closed``. Context exit always closes the socket.
    """

    def __init__(self, url: str, credential: str, state: _State) -> None:
        self._url, self._credential, self._state = url, credential, state
        self._socket: SyncConnection | None = None
        self._context: AbstractContextManager[SyncConnection] | None = None
        self._send_lock = threading.RLock()
        self._receive_lock = threading.Lock()

    @property
    def ready(self) -> SessionReady:
        """Negotiated limits, available after entering the context."""
        if self._state.ready is None:
            raise TazoError("Enter the streaming context first.")
        return self._state.ready

    def __enter__(self) -> StreamingSession:
        if self._state.phase != "new":
            raise TazoError("A streaming session can be entered only once.")
        self._state.phase = "starting"
        try:
            context = sync_connect(
                self._url,
                open_timeout=5,
                close_timeout=2,
                compression=None,
                max_size=256 * 1024,
                max_queue=8,
                user_agent_header=f"tazo-python/{__version__}",
            )
            self._socket = context.__enter__()
            self._context = context
            self._socket.send(self._state.start(self._credential))
            self._credential = ""
            self._state.initialize(self._socket.recv(timeout=self._state.event_timeout))
            return self
        except TazoError:
            self.close()
            raise
        except (OSError, WebSocketException, TimeoutError):
            self.close()
            raise StreamingError(
                "Could not start the streaming session.", code="connection_failed"
            ) from None

    def _connection(self) -> SyncConnection:
        if self._socket is None or self._state.phase in {"new", "closed"}:
            raise TazoError("This streaming session is not open.")
        return self._socket

    def _send(self, data: str | bytes) -> None:
        try:
            self._connection().send(data)
        except (OSError, WebSocketException):
            self.close()
            raise StreamingError(
                "Streaming send failed; audio was not replayed.", code="connection_lost"
            ) from None

    def send_audio(self, audio: bytes) -> None:
        """Send one raw PCM16 message, applying the server-advertised limits."""
        with self._send_lock:
            self._state.validate_audio(audio)
            self._send(audio)
            self._state.audio_bytes += len(audio)
            self._state.phase = "collecting"

    def commit(self) -> None:
        """Complete the turn; consume decision.finalized before sending more audio."""
        with self._send_lock:
            self._send(self._state.control("input.commit"))

    def update(self, *, state: StructuredValue, questions: QuestionsInput) -> None:
        """Replace context and question between turns; consume session.updated."""
        with self._send_lock:
            self._send(self._state.control("session.update", state=state, questions=questions))

    def finish(self) -> None:
        """Request finalization and graceful closure; continue consuming events."""
        with self._send_lock:
            self._send(self._state.control("input.finish"))

    def __next__(self) -> StreamEvent:
        if self._state.phase == "closed":
            raise StopIteration
        if not self._receive_lock.acquire(blocking=False):
            raise TazoError("Only one receiver may iterate a streaming session.")
        try:
            raw = self._connection().recv(timeout=self._state.event_timeout)
            with self._send_lock:
                event = self._state.apply(raw)
            if isinstance(event, SessionClosed):
                self.close()
            return event
        except ConnectionClosed as exc:
            if self._state.phase == "closed":
                raise StopIteration from None
            self.close()
            raise StreamingError(
                "Stream disconnected before session.closed; audio was not replayed.",
                code="connection_lost",
                close_code=exc.rcvd.code if exc.rcvd else None,
            ) from None
        except TimeoutError:
            self.close()
            raise StreamingError(
                "Timed out waiting for a streaming event.", code="receive_timeout"
            ) from None
        except (ProtocolError, StreamingError):
            self.close()
            raise
        finally:
            self._receive_lock.release()

    def close(self) -> None:
        """Close immediately without replaying or cancelling accepted durable work."""
        with self._send_lock:
            self._state.phase = "closed"
            self._credential = ""
            if self._context is not None:
                context, self._context = self._context, None
                context.__exit__(None, None, None)

    def __exit__(self, *args: object) -> None:
        self.close()


class AsyncStreamingSession(AsyncIterator[StreamEvent]):
    """Async context manager and event iterator. Use a concurrent audio-sending task.

    No background reader is created. The websocket's bounded receive queue
    supplies backpressure. One task may receive while another sends. Cancelled
    operations close the socket; accepted durable evaluations may still finish.
    """

    def __init__(self, url: str, credential: str, state: _State) -> None:
        self._url, self._credential, self._state = url, credential, state
        self._socket: AsyncConnection | None = None
        self._send_lock = asyncio.Lock()
        self._receive_lock = asyncio.Lock()

    @property
    def ready(self) -> SessionReady:
        """Negotiated limits, available after entering the context."""
        if self._state.ready is None:
            raise TazoError("Enter the streaming context first.")
        return self._state.ready

    async def __aenter__(self) -> AsyncStreamingSession:
        if self._state.phase != "new":
            raise TazoError("A streaming session can be entered only once.")
        self._state.phase = "starting"
        try:
            self._socket = await async_connect(
                self._url,
                open_timeout=5,
                close_timeout=2,
                compression=None,
                max_size=256 * 1024,
                max_queue=8,
                user_agent_header=f"tazo-python/{__version__}",
            )
            await self._socket.send(self._state.start(self._credential))
            self._credential = ""
            raw = await asyncio.wait_for(self._socket.recv(), self._state.event_timeout)
            self._state.initialize(raw)
            return self
        except (TazoError, asyncio.CancelledError):
            await self.close()
            raise
        except (OSError, WebSocketException, TimeoutError):
            await self.close()
            raise StreamingError(
                "Could not start the streaming session.", code="connection_failed"
            ) from None

    def _connection(self) -> AsyncConnection:
        if self._socket is None or self._state.phase in {"new", "closed"}:
            raise TazoError("This streaming session is not open.")
        return self._socket

    async def _send(self, data: str | bytes) -> None:
        try:
            await self._connection().send(data)
        except asyncio.CancelledError:
            await self.close()
            raise
        except (OSError, WebSocketException):
            await self.close()
            raise StreamingError(
                "Streaming send failed; audio was not replayed.", code="connection_lost"
            ) from None

    async def send_audio(self, audio: bytes) -> None:
        """Send one PCM16 message; the send applies transport backpressure."""
        async with self._send_lock:
            self._state.validate_audio(audio)
            await self._send(audio)
            self._state.audio_bytes += len(audio)
            self._state.phase = "collecting"

    async def commit(self) -> None:
        """Complete the turn; await decision.finalized through the iterator."""
        async with self._send_lock:
            await self._send(self._state.control("input.commit"))

    async def update(self, *, state: StructuredValue, questions: QuestionsInput) -> None:
        """Replace state and questions; await session.updated through the iterator."""
        async with self._send_lock:
            await self._send(
                self._state.control("session.update", state=state, questions=questions)
            )

    async def finish(self) -> None:
        """Request a graceful end; continue iterating until session.closed."""
        async with self._send_lock:
            await self._send(self._state.control("input.finish"))

    async def __anext__(self) -> StreamEvent:
        if self._state.phase == "closed":
            raise StopAsyncIteration
        if self._receive_lock.locked():
            raise TazoError("Only one receiver may iterate a streaming session.")
        async with self._receive_lock:
            try:
                raw = await asyncio.wait_for(self._connection().recv(), self._state.event_timeout)
                async with self._send_lock:
                    event = self._state.apply(raw)
                if isinstance(event, SessionClosed):
                    await self.close()
                return event
            except ConnectionClosed as exc:
                if self._state.phase == "closed":
                    raise StopAsyncIteration from None
                await self.close()
                raise StreamingError(
                    "Stream disconnected before session.closed; audio was not replayed.",
                    code="connection_lost",
                    close_code=exc.rcvd.code if exc.rcvd else None,
                ) from None
            except TimeoutError:
                await self.close()
                raise StreamingError(
                    "Timed out waiting for a streaming event.", code="receive_timeout"
                ) from None
            except (ProtocolError, StreamingError, asyncio.CancelledError):
                await self.close()
                raise

    async def close(self) -> None:
        """Close immediately. It does not cancel already accepted evaluations."""
        self._state.phase = "closed"
        self._credential = ""
        if self._socket is not None:
            await self._socket.close()

    async def __aexit__(self, *args: object) -> None:
        await self.close()
