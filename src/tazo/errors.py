"""Exceptions shared by the sync and async clients."""

from __future__ import annotations


class TazoError(Exception):
    """Base SDK error with recovery metadata and no stored credentials or audio."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        request_id: str | None = None,
        idempotency_key: str | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.request_id = request_id
        self.idempotency_key = idempotency_key
        self.retryable = retryable
        self.retry_after = retry_after


class AuthenticationError(TazoError):
    """Missing or rejected bearer credential."""


class PermissionDeniedError(TazoError):
    """The authenticated caller lacks access."""


class InvalidRequestError(TazoError):
    """Invalid local input or an HTTP 400/422 rejection."""


class NotFoundError(TazoError):
    """The requested model was not found."""


class RequestTooLargeError(InvalidRequestError):
    """Audio or request exceeds the service's size limit."""


class RateLimitError(TazoError):
    """Account admission limit reached; inspect retry_after."""


class IdempotencyConflictError(TazoError):
    """An operation key was reused with different input."""


class DecisionPendingError(TazoError):
    """Accepted work continues. Retain the key and retry the same input later."""


class ConnectionError(TazoError):
    """Transport connection failed; acceptance may be uncertain for writes."""


class TimeoutError(TazoError):
    """A network timeout or the overall operation deadline was reached."""


class ProtocolError(TazoError):
    """The server response doesn't match the public contract."""


class ServiceError(TazoError):
    """The service returned an unsuccessful response."""


class StreamingError(TazoError):
    """A fatal streaming failure or unexpected disconnect; audio is never replayed."""

    def __init__(
        self, message: str, *, code: str | None = None, close_code: int | None = None
    ) -> None:
        super().__init__(message, code=code)
        self.close_code = close_code
