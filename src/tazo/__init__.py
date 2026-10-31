"""Tazo's typed SDK for uploaded and streaming audio decisions."""

from ._audio import wav_chunks, wav_sample_rate
from ._version import __version__
from .client import AsyncTazo, Tazo
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
    StreamingError,
    TazoError,
    TimeoutError,
)
from .types import ChoiceQuestion, NoulQuestion

__all__ = [
    "AsyncTazo",
    "AuthenticationError",
    "ChoiceQuestion",
    "ConnectionError",
    "DecisionPendingError",
    "IdempotencyConflictError",
    "InvalidRequestError",
    "NotFoundError",
    "NoulQuestion",
    "PermissionDeniedError",
    "ProtocolError",
    "RateLimitError",
    "RequestTooLargeError",
    "ServiceError",
    "StreamingError",
    "Tazo",
    "TazoError",
    "TimeoutError",
    "__version__",
    "wav_chunks",
    "wav_sample_rate",
]
