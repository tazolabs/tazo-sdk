import asyncio
import io
import json
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser
from email.utils import format_datetime

import httpx
import pytest
from conftest import QUESTIONS, decision

from tazo import (
    AsyncTazo,
    AuthenticationError,
    ConnectionError,
    DecisionPendingError,
    IdempotencyConflictError,
    InvalidRequestError,
    ProtocolError,
    RateLimitError,
    RequestTooLargeError,
    ServiceError,
    Tazo,
    TazoError,
    TimeoutError,
)
from tazo._http import retry_after


def client(handler, **kwargs):
    transport = httpx.Client(transport=httpx.MockTransport(handler))
    return Tazo(api_key="test-key", http_client=transport, **kwargs)


def multipart(request):
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: " + request.headers["Content-Type"].encode() + b"\r\n\r\n" + request.content
    )
    return {
        part.get_param("name", header="content-disposition"): (
            part.get_payload(decode=True),
            part.get_content_type(),
            part.get_filename(),
        )
        for part in message.iter_parts()
    }


def test_upload_encoding_retries_and_metadata(wav_file):
    seen = []

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(
                504,
                headers={"Retry-After": "0", "X-Tazo-Request-Id": "req_1"},
                json={"error": {"code": "decision_pending", "retryable": True}},
            )
        return httpx.Response(
            200, headers={"X-Tazo-Request-Id": "req_1"}, json=decision(new_field=1)
        )

    with client(handler, base_url="http://test/proxy") as sdk:
        with wav_file.open("rb") as handle:
            result = sdk.decisions.create(audio=handle, state="context", questions=QUESTIONS)
            assert not handle.closed and handle.tell() == 0
    assert len(seen) == 2
    assert seen[0].url == "http://test/proxy/v1/decisions"
    key = seen[0].headers["Idempotency-Key"]
    assert seen[1].headers["Idempotency-Key"] == result.idempotency_key == key
    first, second = map(multipart, seen)
    assert first == second
    assert first["audio"] == (wav_file.read_bytes(), "audio/x-wav", "audio.wav") or first[
        "audio"
    ] == (wav_file.read_bytes(), "audio/wav", "audio.wav")
    assert first["request"][1] == "application/json"
    assert json.loads(first["request"][0])["model"] == "tazo-large"
    assert result.request_id == "req_1" and result.model_extra["new_field"] == 1
    assert "idempotency_key" not in result.model_dump()
    assert seen[0].headers["Authorization"] == "Bearer test-key"
    assert seen[0].headers["User-Agent"].startswith("tazo-python/")


def test_distinct_calls_and_retained_keys():
    seen = []
    sdk = client(
        lambda request: (
            seen.append(request.headers["Idempotency-Key"]) or httpx.Response(200, json=decision())
        )
    )
    for key in (None, None, "retained", "retained"):
        sdk.decisions.create(audio=b"wav", state="s", questions=QUESTIONS, idempotency_key=key)
    assert seen[0] != seen[1]
    assert seen[2:] == ["retained", "retained"]


@pytest.mark.parametrize(
    "status,error_cls",
    [
        (400, InvalidRequestError),
        (401, AuthenticationError),
        (409, IdempotencyConflictError),
        (413, RequestTooLargeError),
        (422, InvalidRequestError),
    ],
)
def test_nonretryable_errors_do_not_loop(status, error_cls):
    calls = []
    sdk = client(
        lambda r: (
            calls.append(r)
            or httpx.Response(
                status,
                json={
                    "error": {
                        "code": "test_code",
                        "message": "A safe message.",
                        "retryable": True,
                    }
                },
            )
        )
    )
    with pytest.raises(error_cls) as exc:
        sdk.decisions.create(audio=b"wav", state="s", questions=QUESTIONS)
    assert exc.value.code == "test_code"
    assert exc.value.idempotency_key and len(calls) == 1


@pytest.mark.parametrize("body", [{"detail": [{"msg": "bad input"}]}, "gateway response"])
def test_unstructured_errors_are_safe(body):
    response = (
        httpx.Response(422, json=body) if isinstance(body, dict) else httpx.Response(502, text=body)
    )
    with pytest.raises(TazoError) as exc:
        client(lambda r: response, max_retries=0).models.list()
    assert exc.value.status_code in {422, 502}
    assert "gateway response" not in str(exc.value)


def test_health_needs_no_key_and_preserves_unhealthy_status(monkeypatch):
    monkeypatch.delenv("TAZO_API_KEY", raising=False)
    seen = []
    transport = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: seen.append(r) or httpx.Response(503, json={"status": "unavailable"})
        )
    )
    with Tazo(http_client=transport) as sdk:
        assert sdk.health.check().status == "unavailable"
        with pytest.raises(AuthenticationError):
            sdk.models.list()
    assert "authorization" not in seen[0].headers
    assert not transport.is_closed
    transport.close()


def test_environment_key_and_owned_client_lifecycle(monkeypatch):
    monkeypatch.setenv("TAZO_API_KEY", "environment-key")
    sdk = Tazo()
    assert sdk._http.credential() == "environment-key"
    sdk.close()
    sdk.close()
    assert sdk._http._client.is_closed
    with pytest.raises(TazoError, match="closed"):
        sdk.health.check()
    assert "environment-key" not in repr(sdk)


@pytest.mark.parametrize("key", ["", " ", "x" * 257, "bad\nkey", "é"])
def test_invalid_idempotency_key_is_local(key):
    sdk = client(lambda r: pytest.fail("Unexpected network call"))
    with pytest.raises(InvalidRequestError):
        sdk.decisions.create(audio=b"wav", state="s", questions=QUESTIONS, idempotency_key=key)


def test_input_validation_and_upload_boundaries():
    sdk = client(lambda r: pytest.fail("Unexpected network call"))
    with pytest.raises(InvalidRequestError):
        sdk.decisions.create(audio=b"wav", state="s", questions={"secret": {"type": "score"}})
    with pytest.raises(InvalidRequestError):
        sdk.decisions.create(audio=b"", state="s", questions=QUESTIONS)
    with pytest.raises(RequestTooLargeError):
        sdk.decisions.create(audio=b"x" * (16 * 1024 * 1024 + 1), state="s", questions=QUESTIONS)
    with pytest.raises(InvalidRequestError):
        sdk.decisions.create(audio=io.StringIO("text"), state="s", questions=QUESTIONS)


def test_explicit_nonretryable_5xx_and_retry_after_budget():
    with pytest.raises(ServiceError) as exc:
        client(lambda r: httpx.Response(502, json={"error": {"retryable": False}})).models.list()
    assert not exc.value.retryable
    calls = []
    sdk = client(
        lambda r: (
            calls.append(r)
            or httpx.Response(
                429,
                headers={"Retry-After": "60"},
                json={"error": {"code": "requests_per_minute_exceeded", "retryable": True}},
            )
        ),
        operation_timeout=0.1,
    )
    with pytest.raises(RateLimitError) as exc:
        sdk.models.list()
    assert exc.value.retry_after == 60 and len(calls) == 1


def test_pending_retains_recovery_metadata():
    sdk = client(
        lambda r: httpx.Response(
            504,
            headers={"Retry-After": "0", "X-Tazo-Request-Id": "req_test"},
            json={"error": {"code": "decision_pending", "retryable": True}},
        ),
        max_retries=1,
    )
    with pytest.raises(DecisionPendingError) as exc:
        sdk.decisions.create(audio=b"wav", state="s", questions=QUESTIONS, idempotency_key="saved")
    assert exc.value.request_id == "req_test" and exc.value.idempotency_key == "saved"


@pytest.mark.parametrize(
    "network_error,error_cls",
    [(httpx.ReadTimeout, TimeoutError), (httpx.ConnectError, ConnectionError)],
)
def test_network_failure_metadata(network_error, error_cls):
    def handler(request):
        raise network_error("private diagnostic", request=request)

    with pytest.raises(error_cls) as exc:
        client(handler, max_retries=0).decisions.create(
            audio=b"wav", state="s", questions=QUESTIONS
        )
    assert exc.value.idempotency_key
    assert "private diagnostic" not in str(exc.value)


@pytest.mark.parametrize(
    "payload",
    [
        decision(answers={"wrong": decision()["answers"]["action"]}),
        decision(usage={"audio_seconds": -1}),
        {"not": "a decision"},
    ],
)
def test_invalid_response_is_protocol_error(payload):
    with pytest.raises(ProtocolError):
        client(lambda r: httpx.Response(200, json=payload)).decisions.create(
            audio=b"wav", state="s", questions=QUESTIONS
        )


def test_noul_and_extensible_model_metadata():
    with client(
        lambda r: httpx.Response(200, json=decision(answers={"yes": {"type": "noul", "noul": 0.8}}))
    ) as sdk:
        result = sdk.decisions.create(
            audio=b"wav",
            state="s",
            questions={"yes": {"type": "noul", "instructions": "Is the request urgent?"}},
        )
        assert result.answers["yes"].noul == 0.8
    model = {
        "id": "future-model",
        "display_name": "Future",
        "description": "Future model",
        "contract": "typed-decisions-v1",
        "ready": True,
        "capabilities": {"question_types": ["future"], "max_questions": 2, "future": 1},
    }
    result = client(lambda r: httpx.Response(200, json={"models": [model]})).models.list()
    assert result.models[0].capabilities.model_extra["future"] == 1


def test_retry_after_dates_and_invalid_values():
    future = format_datetime(datetime.now(UTC) + timedelta(seconds=30))
    assert 28 <= retry_after(future) <= 31
    for value in (None, "garbage", "nan", "inf"):
        assert retry_after(value) is None


def test_async_retry_upload_health_and_ownership():
    async def run():
        calls = []

        async def handler(request):
            calls.append(request)
            if request.url.path == "/healthz":
                return httpx.Response(503, json={"status": "unavailable"})
            if len(calls) == 1:
                return httpx.Response(429, headers={"Retry-After": "0"})
            return httpx.Response(200, json=decision())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
            async with AsyncTazo(api_key="test-key", http_client=transport) as sdk:
                result = await sdk.decisions.create(audio=b"wav", state="s", questions=QUESTIONS)
                assert result.idempotency_key == calls[0].headers["Idempotency-Key"]
                assert multipart(calls[0]) == multipart(calls[1])
                assert (await sdk.health.check()).status == "unavailable"
                assert "authorization" not in calls[-1].headers
            assert not transport.is_closed

    asyncio.run(run())


def test_async_deadline_and_cancellation():
    async def run():
        async def handler(request):
            await asyncio.sleep(10)
            return httpx.Response(200, json=decision())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
            async with AsyncTazo(
                api_key="test-key", http_client=transport, operation_timeout=0.01
            ) as sdk:
                with pytest.raises(TimeoutError) as exc:
                    await sdk.decisions.create(audio=b"wav", state="s", questions=QUESTIONS)
                assert exc.value.idempotency_key
                task = asyncio.create_task(
                    sdk.decisions.create(
                        audio=b"wav", state="s", questions=QUESTIONS, idempotency_key="retained"
                    )
                )
                await asyncio.sleep(0)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    asyncio.run(run())


@pytest.mark.parametrize(
    "base", ["ftp://example.com", "https://user:key@example.com", "https://a/?q=1"]
)
def test_invalid_base_url(base):
    with pytest.raises(ValueError):
        Tazo(base_url=base)
