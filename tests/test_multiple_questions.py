"""Exercise mixed question maps through both public transports and runnable examples."""

import asyncio
import importlib.util
import json
from pathlib import Path

import httpx
import pytest
from conftest import PCM, decision, ready
from test_http import multipart

from tazo import AsyncTazo, ProtocolError, Tazo

ROOT = Path(__file__).resolve().parents[1]


def questions(count):
    return {
        f"q{i}": (
            {"type": "noul", "instructions": "Is the speaker asking a question?"}
            if i % 2
            else {
                "type": "choice",
                "instructions": "Choose a tool.",
                "criteria": {"SEARCH": "Search.", "NO_TOOL": "Wait."},
            }
        )
        for i in range(count)
    }


def answers_for(request):
    answers = {}
    for key, question in request.items():
        if question["type"] == "noul":
            answers[key] = {"type": "noul", "noul": 0.9}
        else:
            choice = next(iter(question["criteria"]))
            answers[key] = {
                "type": "choice",
                "choice": choice,
                "confidence": 1.0,
                "probabilities": {name: float(name == choice) for name in question["criteria"]},
            }
    return answers


def http_handler(calls, *, max_questions=10, omit_answer=False):
    def handle(request):
        calls.append(request)
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "id": "tazo-large",
                            "display_name": "Tazo Large",
                            "description": "Test model",
                            "ready": True,
                            "contract": "typed-decisions-v1",
                            "capabilities": {
                                "question_types": ["choice", "noul"],
                                "max_questions": max_questions,
                            },
                        }
                    ]
                },
            )
        assert request.url.path == "/v1/decisions"
        payload = json.loads(multipart(request)["request"][0])
        answers = answers_for(payload["questions"])
        if omit_answer:
            answers.pop(next(iter(answers)))
        return httpx.Response(200, json=decision(answers=answers))

    return handle


def stream_handler(socket, start):
    manual = start.get("evaluation_mode") == "manual"
    socket.send(
        json.dumps(
            ready(
                audio_scope="turn",
                evaluation_mode="manual" if manual else "interval",
                evaluation_interval_ms=None if manual else 1000,
                **({"audio_retention_ms": 60000} if manual else {}),
            )
        )
    )
    sequence, received = 1, 0

    def emit(kind, **extra):
        nonlocal sequence
        sequence += 1
        socket.send(
            json.dumps(
                {
                    **decision(answers=answers_for(start["questions"])),
                    "type": kind,
                    "session_id": "ses_test",
                    "sequence": sequence,
                    "turn_id": "turn_1",
                    "config_revision": 1,
                    "audio_start_ms": 0,
                    "audio_end_ms": received / 32,
                    **extra,
                }
            )
        )

    for message in socket:
        if isinstance(message, bytes):
            received += len(message)
            if not manual:
                emit("decision.updated")
            continue
        control = json.loads(message)
        if control["type"] == "input.commit":
            extra = {}
            if manual:
                extra["input_audio_range"] = {
                    "start_sample": control["audio_start_sample"],
                    "end_sample": control["audio_end_sample"],
                    "sample_rate_hz": 16000,
                }
            emit("decision.finalized", **extra)
        elif control["type"] == "input.finish":
            sequence += 1
            socket.send(
                json.dumps(
                    {
                        "type": "session.closed",
                        "session_id": "ses_test",
                        "sequence": sequence,
                    }
                )
            )
            return


@pytest.mark.parametrize("count", [1, 2, 10])
@pytest.mark.parametrize("async_mode", [False, True])
def test_upload_preserves_every_mixed_question(wav_file, count, async_mode):
    request, calls = questions(count), []
    handler = http_handler(calls)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
            async with AsyncTazo(api_key="test-key", http_client=transport) as client:
                return await client.decisions.create(
                    audio=wav_file, state="Shared.", questions=request
                )

    if async_mode:
        result = asyncio.run(run())
    else:
        with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
            with Tazo(api_key="test-key", http_client=transport) as client:
                result = client.decisions.create(audio=wav_file, state="Shared.", questions=request)
    assert result.model_dump()["answers"] == answers_for(request)
    assert len(calls) == 1  # One audio upload, rather than one call per question.
    assert json.loads(multipart(calls[0])["request"][0])["questions"] == request


@pytest.mark.parametrize("count", [1, 2, 10])
@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("mode", ["interval", "manual"])
def test_stream_returns_every_question_for_one_audio_range(
    websocket_server, count, async_mode, mode
):
    request = questions(count)
    options = {"audio_scope": "turn", "evaluation_mode": mode}
    commit = {"audio_start_sample": 0, "audio_end_sample": 1600} if mode == "manual" else {}

    async def run(url):
        async with AsyncTazo(api_key="test-key", base_url=url) as client:
            async with client.decisions.stream(
                state="Shared.", questions=request, **options
            ) as session:
                await session.send_audio(PCM)
                await session.commit(**commit)
                if mode == "interval":
                    assert (await anext(session)).model_dump()["answers"] == answers_for(request)
                final = await anext(session)
                await session.finish()
                assert [event.type async for event in session] == ["session.closed"]
                return final

    with websocket_server(stream_handler) as (url, calls):
        if async_mode:
            final = asyncio.run(run(url))
        else:
            with Tazo(api_key="test-key", base_url=url) as client:
                with client.decisions.stream(
                    state="Shared.", questions=request, **options
                ) as session:
                    session.send_audio(PCM)
                    session.commit(**commit)
                    if mode == "interval":
                        assert next(session).model_dump()["answers"] == answers_for(request)
                    final = next(session)
                    session.finish()
                    assert [event.type for event in session] == ["session.closed"]
        assert calls[0]["questions"] == request
    assert final.type == "decision.finalized"
    assert final.model_dump()["answers"] == answers_for(request)
    if mode == "manual":
        assert final.input_audio_range.end_sample == 1600


def test_upload_refuses_an_incomplete_multiquestion_response(wav_file):
    with httpx.Client(
        transport=httpx.MockTransport(http_handler([], omit_answer=True))
    ) as transport:
        with Tazo(api_key="test-key", http_client=transport) as client:
            with pytest.raises(ProtocolError, match="question IDs"):
                client.decisions.create(audio=wav_file, state="Shared.", questions=questions(2))


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("mode", ["upload", "stream"])
def test_runnable_example_uses_both_answers(wav_file, websocket_server, async_mode, mode):
    spec = importlib.util.spec_from_file_location(
        "multiquestion_example", ROOT / "examples/multiple_questions.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    handler = http_handler([])

    async def run(url):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
            async with AsyncTazo(api_key="test-key", base_url=url, http_client=transport) as client:
                return await module.run_async(client, wav_file, mode)

    with websocket_server(stream_handler) as (url, _):
        if async_mode:
            result = asyncio.run(run(url))
        else:
            with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
                with Tazo(api_key="test-key", base_url=url, http_client=transport) as client:
                    result = module.run_sync(client, wav_file, mode)
    assert result.model_dump()["answers"] == answers_for(module.QUESTIONS)


def test_example_stops_before_upload_on_an_older_deployment(wav_file):
    spec = importlib.util.spec_from_file_location(
        "multiquestion_example", ROOT / "examples/multiple_questions.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    with httpx.Client(
        transport=httpx.MockTransport(http_handler(calls, max_questions=1))
    ) as transport:
        with Tazo(api_key="test-key", http_client=transport) as client:
            with pytest.raises(RuntimeError, match="not enabled multiple questions"):
                module.run_sync(client, wav_file, "upload")
    assert [call.url.path for call in calls] == ["/v1/models"]
