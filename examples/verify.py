"""Verify sync/async uploads and streaming with the installed Tazo SDK.

Requires Python 3.11+, tazo-sdk, TAZO_API_KEY, and a mono 16 kHz PCM16 WAV.
Runs real inference work, sequentially, for each requested model. Exit code 0
means every case passed; 1 means a check failed; 2 means invalid local input.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import math
import os
import queue
import sys
import threading
import time
import wave
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import asdict, dataclass
from functools import partial
from pathlib import Path

import tazo
from tazo import AsyncTazo, Tazo, TazoError
from tazo.types import (
    ChoiceAnswer,
    DecisionEvent,
    DecisionResponse,
    ErrorEvent,
    ModelList,
    QuestionsInput,
    SessionClosed,
    SessionReady,
)

STATE = "Route the spoken request to the weather tool when weather information is requested."
QUESTIONS: QuestionsInput = {
    "action": {
        "type": "choice",
        "instructions": "Select get_weather for a weather request; otherwise select NO_TOOL.",
        "criteria": {
            "get_weather": "The speaker requests weather information.",
            "NO_TOOL": "The speaker is not requesting weather information.",
        },
    }
}
CHOICES = {"get_weather", "NO_TOOL"}


@dataclass(frozen=True)
class Audio:
    filename: str
    wav: bytes
    pcm: bytes

    @property
    def duration_ms(self) -> float:
        return len(self.pcm) / 32

    def chunks(self, ready: SessionReady) -> Iterator[bytes]:
        if self.duration_ms > ready.max_turn_audio_ms:
            raise ValueError("Audio exceeds the streaming turn limit; use a shorter recording.")
        size = min(3200, ready.max_audio_message_bytes // 2 * 2)
        if size < 2:
            raise ValueError("Server advertised an invalid PCM message limit.")
        for offset in range(0, len(self.pcm), size):
            yield self.pcm[offset : offset + size]


def load_audio(path: Path) -> Audio:
    with path.open("rb") as source:
        data = source.read(16 * 1024 * 1024 + 1)
    if len(data) > 16 * 1024 * 1024:
        raise ValueError("Audio uploads must fit within 16 MiB.")
    with wave.open(io.BytesIO(data), "rb") as source:
        if (
            source.getnchannels(),
            source.getsampwidth(),
            source.getframerate(),
            source.getcomptype(),
        ) != (1, 2, 16000, "NONE"):
            raise ValueError("Use a mono, 16 kHz, PCM16 WAV file.")
        frames = source.getnframes()
        pcm = source.readframes(frames)
        if not frames or len(pcm) != frames * 2:
            raise ValueError("Audio is empty or the WAV is truncated.")
    return Audio(path.name, data, pcm)


def check_models(models: ModelList, selected: list[str], audio: Audio) -> None:
    available = {model.id: model for model in models.models}
    for name in selected:
        if name not in available or not available[name].ready:
            raise RuntimeError(f"Model {name} is missing or not ready.")
        capabilities = available[name].capabilities
        if "choice" not in capabilities.question_types:
            raise RuntimeError(f"Model {name} does not advertise choice questions.")
        if (
            capabilities.max_audio_seconds is not None
            and audio.duration_ms > capabilities.max_audio_seconds * 1000
        ):
            raise ValueError(f"Audio exceeds the advertised limit for {name}.")


def validate_result(result: DecisionResponse, model: str, expected: str | None) -> None:
    if result.model != model or set(result.answers) != {"action"}:
        raise RuntimeError("Response model or question IDs do not match the request.")
    answer = result.answers["action"]
    if not isinstance(answer, ChoiceAnswer) or set(answer.probabilities) != CHOICES:
        raise RuntimeError("The result does not contain the requested choice options.")
    if expected is not None and answer.choice != expected:
        raise RuntimeError(f"Expected {expected}, received {answer.choice}.")


def final_result(final: DecisionEvent | None, closed: bool, audio: Audio) -> DecisionEvent:
    if final is None or not closed:
        raise RuntimeError("Streaming must produce decision.finalized and session.closed.")
    if final.audio_start_ms != 0 or abs(final.audio_end_ms - audio.duration_ms) > 1:
        raise RuntimeError("The final decision does not cover the complete recording.")
    return final


def sync_upload(client: Tazo, audio: Audio, model: str) -> DecisionResponse:
    return client.decisions.create(
        audio=audio.wav,
        filename=audio.filename,
        content_type="audio/wav",
        model=model,
        state=STATE,
        questions=QUESTIONS,
    )


async def async_upload(client: AsyncTazo, audio: Audio, model: str) -> DecisionResponse:
    return await client.decisions.create(
        audio=audio.wav,
        filename=audio.filename,
        content_type="audio/wav",
        model=model,
        state=STATE,
        questions=QUESTIONS,
    )


def sync_stream(client: Tazo, audio: Audio, model: str, timeout: float) -> DecisionResponse:
    final = None
    closed = False
    failures: queue.Queue[Exception] = queue.Queue()
    stopped = threading.Event()
    with client.decisions.stream(
        model=model,
        state=STATE,
        questions=QUESTIONS,
        audio_scope="turn",
        evaluation_interval_ms=1000,
        event_timeout=timeout,
    ) as session:
        # Validate all negotiated limits before starting the sender thread.
        chunks = list(audio.chunks(session.ready))

        def send() -> None:
            try:
                for chunk in chunks:
                    if stopped.is_set():
                        return
                    session.send_audio(chunk)
                    if stopped.wait(len(chunk) / 32000):
                        return
                session.commit()
            except Exception as error:
                failures.put(error)
                session.close()  # Unblock the receiver when the producer fails.

        sender = threading.Thread(target=send, name="tazo-audio-sender", daemon=True)
        sender.start()
        deadline = time.monotonic() + timeout
        try:
            for event in session:
                if time.monotonic() > deadline:
                    raise TimeoutError("The sync streaming verification exceeded its time budget.")
                if isinstance(event, ErrorEvent):
                    raise RuntimeError(f"Stream error: {event.error.code}: {event.error.message}")
                if isinstance(event, DecisionEvent) and event.type == "decision.finalized":
                    if final is not None:
                        raise RuntimeError("Received more than one final decision for this turn.")
                    final = event
                    session.finish()
                if isinstance(event, SessionClosed):
                    closed = True
        finally:
            stopped.set()
            session.close()
            sender.join(timeout=5)
            if sender.is_alive():
                raise RuntimeError("Audio sender did not shut down.")
            if not failures.empty():
                raise failures.get_nowait()
    return final_result(final, closed, audio)


async def async_stream(
    client: AsyncTazo, audio: Audio, model: str, timeout: float
) -> DecisionResponse:
    final = None
    closed = False
    async with (
        asyncio.timeout(timeout),
        client.decisions.stream(
            model=model,
            state=STATE,
            questions=QUESTIONS,
            audio_scope="turn",
            evaluation_interval_ms=1000,
            event_timeout=timeout,
        ) as session,
    ):
        chunks = list(audio.chunks(session.ready))

        async def send() -> None:
            for chunk in chunks:
                await session.send_audio(chunk)
                await asyncio.sleep(len(chunk) / 32000)
            await session.commit()

        async def receive() -> None:
            nonlocal final, closed
            async for event in session:
                if isinstance(event, ErrorEvent):
                    raise RuntimeError(f"Stream error: {event.error.code}: {event.error.message}")
                if isinstance(event, DecisionEvent) and event.type == "decision.finalized":
                    if final is not None:
                        raise RuntimeError("Received more than one final decision for this turn.")
                    final = event
                    await session.finish()
                if isinstance(event, SessionClosed):
                    closed = True
            final_result(final, closed, audio)

        async with asyncio.TaskGroup() as group:
            group.create_task(send())
            group.create_task(receive())
    return final_result(final, closed, audio)


@dataclass
class CaseResult:
    model: str
    mode: str
    passed: bool
    seconds: float
    decision: dict[str, object] | None = None
    error: str | None = None


def error_message(error: Exception) -> str:
    if isinstance(error, ExceptionGroup):
        return "; ".join(error_message(child) for child in error.exceptions)
    if isinstance(error, TazoError):
        return (
            f"{type(error).__name__}: {error} (code={error.code}, "
            f"request_id={error.request_id}, retry_after={error.retry_after})"
        )
    return f"{type(error).__name__}: {error}"


def checked_case(
    model: str,
    mode: str,
    started: float,
    decision: DecisionResponse,
    expected: str | None,
) -> CaseResult:
    payload = decision.model_dump(mode="json")
    if decision.request_id is not None:
        payload["request_id"] = decision.request_id
    if decision.idempotency_key is not None:
        payload["idempotency_key"] = decision.idempotency_key
    result = CaseResult(model, mode, True, time.monotonic() - started, payload)
    try:
        validate_result(decision, model, expected)
    except Exception as error:
        result.passed = False
        result.error = error_message(error)
    label = "PASS" if result.passed else "FAIL"
    print(f"{label} {model} / {mode} ({result.seconds:.2f}s)", flush=True)
    if result.error:
        print(result.error, flush=True)
    print(json.dumps(result.decision, indent=2), flush=True)
    return result


def failed_case(model: str, mode: str, started: float, error: Exception) -> CaseResult:
    result = CaseResult(model, mode, False, time.monotonic() - started, error=error_message(error))
    print(f"FAIL {model} / {mode}: {result.error}", flush=True)
    return result


def record(
    model: str,
    mode: str,
    started: float,
    operation: Callable[[], DecisionResponse],
    expected: str | None,
) -> CaseResult:
    try:
        return checked_case(model, mode, started, operation(), expected)
    except Exception as error:
        return failed_case(model, mode, started, error)


async def async_cases(
    audio: Audio, models: list[str], base_url: str, timeout: float, expected: str | None
) -> list[CaseResult]:
    results = []
    async with AsyncTazo(base_url=base_url, operation_timeout=timeout) as client:
        if (await client.health.check()).status != "ok":
            raise RuntimeError("Async health check reported unavailable.")
        check_models(await client.models.list(), models, audio)
        for model in models:
            for mode in ("async-upload", "async-stream"):
                print(f"RUN  {model} / {mode}", flush=True)
                started = time.monotonic()
                try:
                    operation: Awaitable[DecisionResponse]
                    if mode == "async-upload":
                        operation = async_upload(client, audio, model)
                    else:
                        operation = async_stream(client, audio, model, timeout)
                    decision = await operation
                    results.append(checked_case(model, mode, started, decision, expected))
                except Exception as error:
                    results.append(failed_case(model, mode, started, error))
    return results


def verify(
    audio: Audio, models: list[str], base_url: str, timeout: float, expected: str | None
) -> list[CaseResult]:
    results = []
    with Tazo(base_url=base_url, operation_timeout=timeout) as client:
        if client.health.check().status != "ok":
            raise RuntimeError("Sync health check reported unavailable.")
        check_models(client.models.list(), models, audio)
        for model in models:
            for mode in ("sync-upload", "sync-stream"):
                print(f"RUN  {model} / {mode}", flush=True)
                operation = (
                    partial(sync_upload, client, audio, model)
                    if mode == "sync-upload"
                    else partial(sync_stream, client, audio, model, timeout)
                )
                results.append(record(model, mode, time.monotonic(), operation, expected))
    results.extend(asyncio.run(async_cases(audio, models, base_url, timeout, expected)))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", required=True, type=Path)
    parser.add_argument("--base-url", default="https://api.tazolabs.com")
    parser.add_argument("--models", nargs="+", default=["tazo-large", "tazo-small"])
    parser.add_argument("--expected-choice", choices=sorted(CHOICES))
    parser.add_argument(
        "--timeout", type=float, default=180, help="HTTP budget/stream wait seconds"
    )
    parser.add_argument("--output", type=Path, help="Save decisions and the summary as JSON")
    args = parser.parse_args()
    try:
        if not os.environ.get("TAZO_API_KEY", "").strip():
            raise ValueError("Set TAZO_API_KEY to a key created in the Tazo console.")
        if not math.isfinite(args.timeout) or args.timeout <= 0:
            raise ValueError("--timeout must be positive and finite.")
        audio = load_audio(args.audio)
        if args.output is not None:
            if args.output.resolve() == args.audio.resolve():
                raise ValueError("The JSON report must not overwrite the audio file.")
            if not args.output.parent.is_dir():
                raise ValueError("The report's parent directory does not exist.")
    except (OSError, ValueError, wave.Error, EOFError) as error:
        parser.error(str(error))
    print(f"SDK {tazo.__version__} from {tazo.__file__}")
    print(f"Target: {args.base_url}; audio: {audio.duration_ms / 1000:.2f}s")
    models = list(dict.fromkeys(args.models))
    try:
        results = verify(audio, models, args.base_url, args.timeout, args.expected_choice)
        passed = sum(result.passed for result in results)
        print(f"\nSummary: {passed}/{len(results)} passed")
        if args.expected_choice is None:
            print("Checks cover the API flow. Use --expected-choice to check the answer too.")
        if args.output is not None:
            report = {
                "sdk_version": tazo.__version__,
                "sdk_import_path": tazo.__file__,
                "base_url": args.base_url,
                "expected_choice": args.expected_choice,
                "passed": passed,
                "total": len(results),
                "results": [asdict(result) for result in results],
            }
            args.output.write_text(json.dumps(report, indent=2) + "\n")
            print(f"Report: {args.output}")
        return 0 if passed == len(models) * 4 else 1
    except Exception as error:
        print(f"Verification stopped: {error_message(error)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
