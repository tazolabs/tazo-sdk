# Tazo Python SDK

Typed, synchronous and asynchronous clients for Tazo audio decisions.
Supports exactly four public operations: uploaded decisions, streaming decisions,
model discovery, and health. API-key management and internal APIs are outside this package.

## Install

Python 3.11 or newer is required. Install [tazo-sdk from PyPI](https://pypi.org/project/tazo-sdk/):

```sh
python -m pip install tazo-sdk
export TAZO_API_KEY='your-key'
```

Sign in to the [Tazo console](https://app.tazolabs.com), open **API keys**, and
create a key. Set `TAZO_API_KEY` to the complete key, including its `tazo_sk_`
prefix. The SDK reads this environment variable or accepts `Tazo(api_key="...")`.
See the [Python SDK documentation](https://docs.tazolabs.com/sdk/python/overview)
for installation, examples, and API reference.

## Uploaded decision

```python
from pathlib import Path
from tazo import Tazo

questions = {
    "action": {
        "type": "choice",
        "instructions": "Which action should be taken?",
        "criteria": {
            "get_weather": "Get weather for a city.",
            "NO_TOOL": "No tool is needed.",
        },
    }
}

with Tazo() as client:
    result = client.decisions.create(
        audio=Path("request.wav"),
        state="Choose the action matching the spoken request.",
        questions=questions,
    )
    print(result.answers["action"])
    print(result.request_id, result.idempotency_key)
```

`model` defaults to `tazo-large`. Choice and noul dictionaries or the optional
`ChoiceQuestion` / `NoulQuestion` models are accepted. Check `models.list()` for
model-specific capabilities. Score questions aren't supported by this SDK release.

Audio can be a path, bytes, or a binary stream. For bytes, pass `filename` and
`content_type` when known. Uploads are snapshotted once, up to 16 MiB, so retries
use the same bytes and MIME type. Caller-provided streams aren't closed;
seekable streams are restored to their initial position. Nonseekable streams are
consumed once. No prompt word limit is imposed by the SDK.

## Models and health

```python
from tazo import Tazo

with Tazo() as client:
    print(client.health.check().status)  # ok or unavailable; no auth sent
    for model in client.models.list().models:
        print(model.id, model.ready, model.capabilities.question_types)
```

Credentials are required only for protected calls. HTTP 503 with the documented
unavailable health body returns `HealthStatus(status="unavailable")`.

## Async

```python
import asyncio
from tazo import AsyncTazo


async def main():
    async with AsyncTazo() as client:
        print((await client.models.list()).models)


asyncio.run(main())
```

Await `decisions.create`, `models.list`, and `health.check`. Async streaming is an
async context manager, not an awaitable factory.

## Streaming

Both clients expose `decisions.stream(state=..., questions=...)`. Entering the
context authenticates in `session.start` and waits for `session.ready`. Inspect
`session.ready` for limits. Send raw mono 16 kHz PCM16 little-endian bytes, at most
`max_audio_message_bytes` per message. The `wav_chunks` helper strips WAV headers
and validates the format; it does not resample or pace audio.

Send audio in one task while another consumes events:

```python
import asyncio
from tazo import AsyncTazo, wav_chunks


async def stream_recording(questions):
    async with AsyncTazo() as client:
        async with client.decisions.stream(
            state="Choose an action.", questions=questions, audio_scope="turn"
        ) as session:

            async def send():
                for chunk in wav_chunks("request.wav"):
                    await asyncio.sleep(len(chunk) / 32000)  # microphone pacing
                    await session.send_audio(chunk)
                await session.commit()

            async def receive():
                async for event in session:
                    if event.type in {"decision.updated", "decision.finalized"}:
                        print(event.model_dump())
                    if event.type == "decision.finalized":
                        await session.finish()

            async with asyncio.TaskGroup() as group:
                group.create_task(send())
                group.create_task(receive())
```

The executable `examples/stream.py` also propagates producer failures immediately.
For synchronous streaming, use a sender thread and iterate the session in the
receiving thread. Never receive concurrently from multiple tasks or threads.

`commit()` sends a control message; consume `decision.finalized` before another
turn. `update(state=..., questions=...)` is allowed between turns; consume
`session.updated` before audio. `finish()` requests finalization and closure;
continue iterating through `session.closed`. Context exit closes immediately.
Fatal server errors raise `StreamingError`; recoverable errors are `ErrorEvent`s.
Future event types become `UnknownEvent`, preserving their fields.

Default `audio_scope="turn"` evaluates cumulative audio. `chunk` evaluates only
new audio; its final result covers the last window, not the whole turn. Each
evaluation counts against the account's per-model limits. Long recordings must
be divided into turns within the server's advertised duration. There is no
automatic reconnect, audio replay, silence detection, or microphone dependency.

## Retries and errors

Each `decisions.create()` call generates a UUID operation key, unless you supply
`idempotency_key`. All automatic attempts reuse the key, JSON, and audio. A new
method call creates a new operation unless you explicitly reuse a retained key.
Use a caller-generated key if you need recovery across process crashes or async
cancellation. Accepted durable work can continue after a disconnect.

```python
from pathlib import Path
from uuid import uuid4
from tazo import DecisionPendingError, Tazo

key = str(uuid4())  # persist alongside your original request before submission
with Tazo() as client:
    try:
        result = client.decisions.create(
            audio=Path("request.wav"),
            state="Choose an action.",
            questions=questions,
            idempotency_key=key,
        )
    except DecisionPendingError as error:
        print(error.request_id, error.idempotency_key)
        # Later: call create with this key and the exact same input and MIME type.
```

Default `max_retries=2` means at most three attempts. Retryable service/network
errors use bounded exponential backoff with jitter and respect `Retry-After`.
Authentication, invalid input, conflicts, and oversized uploads are not retried.
`decision_pending` may be retried with the retained key. There is no job-status
endpoint. Recovery requires calling create again with the original input.

Exceptions expose `status_code`, `code`, `request_id`, `idempotency_key`,
`retryable`, and `retry_after` where available. Invalid server responses raise
`ProtocolError`. HTTP errors without JSON receive a safe generic message.
The SDK does not log request content, credentials, or audio. Don't enable wire
debug logging in applications that must keep these private.

## Configuration and lifecycle

Defaults: `base_url="https://api.tazolabs.com"`, connect timeout 5 seconds,
read timeout 65 seconds, write timeout 30 seconds, pool timeout 5 seconds, and
`operation_timeout=180` seconds. Set a float or an `httpx.Timeout` for `timeout`.

The operation timeout bounds the HTTP retry budget and caps each network phase
by the remaining time. Sync calls cannot forcibly interrupt a response that keeps
delivering chunks or spans multiple phases; async HTTP additionally enforces a
hard per-attempt deadline within the remaining budget. Upload snapshotting is
before this budget. Streaming has its own `event_timeout` (default 65 seconds),
5-second connection timeout, and 2-second close timeout. All are seconds.

Custom `base_url` supports local HTTP and path-prefixed proxies; omit `/v1`.
Use `max_retries=0` with direct-inference development servers that don't persist
idempotency keys. Safe replay requires a target honoring durable idempotency.
WebSocket URLs are derived as ws/wss from that URL. Redirects aren't followed.
Clients are reusable connection pools; close them after their streaming contexts.
An injected `http_client` remains caller-owned. Injected clients should not have
default authorization headers if health requests must remain unauthenticated.

## Verify the published SDK

This checkout prepares **0.1.2rc1**, a release candidate for testing the public
repository's publishing workflow. The stable **0.1.1** package remains available
on PyPI and TestPyPI. The candidate's runtime files match that release byte for
byte except for `__version__`; CI checks them against the recorded hashes in
[compatibility/published-0.1.1.json](compatibility/published-0.1.1.json).

Follow [WORKFLOW_TEST.md](WORKFLOW_TEST.md) to publish and install the candidate.
Ordinary `pip install tazo-sdk` selects the stable release; test the candidate
with an explicit `tazo-sdk==0.1.2rc1` pin after its publication.

With the published package installed and `TAZO_API_KEY` set, run from this checkout:

```sh
python examples/verify.py --audio examples/weather.wav \
  --expected-choice get_weather --output ./tazo-sdk-verification.json
```

This exercises sync upload, sync streaming, async upload, and async streaming
for both models. It submits real inference work. Expect `Summary: 8/8 passed`
for the supplied weather recording. See [examples/README.md](examples/README.md)
for audio requirements, expected-answer checks, and JSON reports.

## Develop

Use Python 3.11+ and [uv](https://docs.astral.sh/uv/). From the repository root:

```sh
uv sync --frozen --all-groups
uv run --frozen pytest
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen mypy src/tazo examples
uv build --no-sources
uv run --frozen --group release python -m twine check dist/*
```

The public repository is self-contained. Tests use HTTP mocks and local
HTTP/WebSocket servers without live credentials or the serving backend.
CI separately installs the built wheel to prevent an editable checkout or
backend dependencies from masking packaging mistakes. The only runtime
dependencies are HTTPX, Pydantic, and websockets.

| Directory | Contents |
| --- | --- |
| `src/tazo` | The typed synchronous and asynchronous SDK; 0.1.2rc1 changes only the runtime version identifier. |
| `examples` | Upload, streaming, durable replay smoke test, and four-mode verification with sample audio. |
| `tests` | HTTP, streaming, public-contract, documentation, verification, and release-baseline tests. |
| `api` | Public OpenAPI and AsyncAPI contracts used in compatibility tests. |
| `docs/source` | Sphinx API reference sources. |
| `docs/mintlify` | SDK customer guides and generated reference pages for the documentation site. |
| `scripts` | Reference generation and release validation. |
| `.github/workflows` | Installed-wheel CI and tag-gated OIDC publication. |

See [docs/README.md](docs/README.md) to generate documentation. The public
[agent skill](https://github.com/tazolabs/agent-skills) can also help an agent
set up and use this SDK.

## Repository setup and releases

Maintainers moving publication to this repository should follow [SETUP.md](SETUP.md).
Existing users can continue installing `tazo-sdk==0.1.1`; no reinstall or new
package release is needed because the source repository became public.
Do not republish 0.1.1. The release workflow rejects already-published 0.1.0
and 0.1.1. Use [the release-candidate checklist](WORKFLOW_TEST.md) to test the new
workflow with 0.1.2rc1. A later stable release can use 0.1.2 after review.

See [RELEASING.md](RELEASING.md) for future releases. API v1 and SDK versions are
independent. This beta may change its Python interface before 1.0; changes are
recorded in [CHANGELOG.md](CHANGELOG.md). Response fields are additive-compatible.
