import asyncio
import json
import threading

import pytest
from conftest import PCM, QUESTIONS, decision, ready

from tazo import AsyncTazo, InvalidRequestError, ProtocolError, StreamingError, Tazo, wav_chunks
from tazo.types import ErrorEvent, UnknownEvent


@pytest.mark.parametrize("scope", [None, "turn"])
@pytest.mark.parametrize("mode", [None, "interval"])
def test_default_sdk_preserves_legacy_start_and_commit_messages(websocket_server, scope, mode):
    options = {}
    if scope is not None:
        options["audio_scope"] = scope
    if mode is not None:
        options["evaluation_mode"] = mode
    with websocket_server() as (url, calls), Tazo(base_url=url, api_key="test-key") as sdk:
        with sdk.decisions.stream(state="context", questions=QUESTIONS, **options) as session:
            session.send_audio(PCM)
            session.commit()
            assert next(session).type == "decision.updated"
            assert next(session).type == "decision.finalized"
            session.finish()
            assert [event.type for event in session] == ["session.closed"]
    assert calls[0] == {
        "type": "session.start",
        "authorization": "Bearer test-key",
        "model": "tazo-large",
        "state": "context",
        "questions": QUESTIONS,
        "audio_scope": scope or "chunk",
        "input_audio": {"encoding": "pcm_s16le", "sample_rate_hz": 16000, "channels": 1},
    }
    assert [json.loads(call) for call in calls[1:] if isinstance(call, str)] == [
        {"type": "input.commit"},
        {"type": "input.finish"},
    ]


@pytest.mark.parametrize("async_mode", [False, True])
def test_manual_stream_preserves_connection_sample_clock_and_allows_audio_during_inference(
    websocket_server, async_mode
):
    def handler(socket, start):
        assert start["evaluation_mode"] == "manual"
        assert "evaluation_interval_ms" not in start
        socket.send(
            json.dumps(
                ready(
                    evaluation_mode="manual", evaluation_interval_ms=None, audio_retention_ms=60000
                )
            )
        )
        assert socket.recv() == PCM
        assert json.loads(socket.recv()) == {
            "type": "input.commit",
            "audio_start_sample": 0,
            "audio_end_sample": 1600,
        }
        assert socket.recv() == PCM  # Remains legal before the result is read.
        for turn, first, last, sequence in [(1, 0, 1600, 2), (2, 1600, 3200, 3)]:
            if turn == 2:
                assert json.loads(socket.recv()) == {
                    "type": "input.commit",
                    "audio_start_sample": first,
                    "audio_end_sample": last,
                }
            socket.send(
                json.dumps(
                    decision(
                        type="decision.finalized",
                        session_id="ses_test",
                        sequence=sequence,
                        turn_id=f"turn_{turn}",
                        config_revision=1,
                        audio_start_ms=0,
                        audio_end_ms=100,
                        input_audio_range={
                            "start_sample": first,
                            "end_sample": last,
                            "sample_rate_hz": 16000,
                        },
                    )
                )
            )
        assert json.loads(socket.recv()) == {"type": "input.finish"}
        socket.send(json.dumps({"type": "session.closed", "session_id": "ses_test", "sequence": 4}))

    async def run(url):
        async with AsyncTazo(base_url=url, api_key="test-key") as sdk:
            async with sdk.decisions.stream(
                state="s", questions=QUESTIONS, audio_scope="turn", evaluation_mode="manual"
            ) as session:
                await session.send_audio(PCM)
                with pytest.raises(InvalidRequestError):
                    await session.commit()
                await session.commit(audio_start_sample=0, audio_end_sample=1600)
                await session.send_audio(PCM)
                first = await anext(session)
                assert first.input_audio_range.start_sample == 0
                await session.commit(audio_start_sample=1600, audio_end_sample=3200)
                assert (await anext(session)).input_audio_range.end_sample == 3200
                await session.finish()
                assert [event.type async for event in session] == ["session.closed"]

    with websocket_server(handler) as (url, _):
        if async_mode:
            asyncio.run(run(url))
        else:
            with Tazo(base_url=url, api_key="test-key") as sdk:
                with sdk.decisions.stream(
                    state="s", questions=QUESTIONS, audio_scope="turn", evaluation_mode="manual"
                ) as session:
                    session.send_audio(PCM)
                    with pytest.raises(InvalidRequestError):
                        session.commit()
                    session.commit(audio_start_sample=0, audio_end_sample=1600)
                    session.send_audio(PCM)
                    assert next(session).input_audio_range.start_sample == 0
                    session.commit(audio_start_sample=1600, audio_end_sample=3200)
                    assert next(session).input_audio_range.end_sample == 3200
                    session.finish()
                    assert [event.type for event in session] == ["session.closed"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"audio_scope": "chunk", "evaluation_mode": "manual"},
        {"audio_scope": "turn", "evaluation_mode": "manual", "evaluation_interval_ms": 1000},
        {"evaluation_mode": "unknown"},
    ],
)
def test_sdk_rejects_invalid_evaluation_mode_configuration(overrides):
    with Tazo(api_key="test-key") as sdk:
        with pytest.raises(InvalidRequestError):
            sdk.decisions.stream(state="s", questions=QUESTIONS, **overrides)


@pytest.mark.parametrize("scope", ["turn", "chunk"])
def test_sync_stream_turns_updates_and_limits(websocket_server, scope):
    with websocket_server() as (url, calls), Tazo(base_url=url, api_key="test-key") as sdk:
        with sdk.decisions.stream(
            state="context", questions=QUESTIONS, audio_scope=scope
        ) as session:
            assert session.ready.max_audio_message_bytes == 6400
            with pytest.raises(InvalidRequestError):
                session.commit()
            with pytest.raises(InvalidRequestError):
                session.send_audio(PCM * 3)
            session.send_audio(PCM)
            session.commit()
            with pytest.raises(InvalidRequestError):
                session.send_audio(PCM)
            assert next(session).type == "decision.updated"
            assert next(session).type == "decision.finalized"
            session.update(state={"new": "context"}, questions=QUESTIONS)
            with pytest.raises(InvalidRequestError):
                session.send_audio(PCM)
            assert next(session).type == "session.updated"

            # A sender thread can run while the receiving thread iterates.
            def send():
                session.send_audio(PCM)
                session.commit()

            sender = threading.Thread(target=send)
            sender.start()
            assert next(session).type == "decision.updated"
            assert next(session).type == "decision.finalized"
            sender.join(timeout=3)
            assert not sender.is_alive()
            session.finish()
            assert [event.type for event in session] == ["session.closed"]
        with pytest.raises(Exception, match="once"):
            session.__enter__()
    assert calls[0]["type"] == "session.start"
    assert calls[0]["audio_scope"] == scope
    assert [value for value in calls if isinstance(value, bytes)] == [PCM, PCM]


def test_async_stream_concurrent_tasks(websocket_server):
    async def run(url):
        async with AsyncTazo(base_url=url, api_key="test-key") as sdk:
            async with sdk.decisions.stream(state="s", questions=QUESTIONS) as session:

                async def send():
                    for _ in range(3):
                        await session.send_audio(PCM)
                        await asyncio.sleep(0.01)
                    await session.commit()

                sender = asyncio.create_task(send())
                events = []
                async for event in session:
                    events.append(event)
                    if event.type == "decision.finalized":
                        await session.finish()
                await sender
                assert [e.type for e in events] == ["decision.updated"] * 3 + [
                    "decision.finalized",
                    "session.closed",
                ]
                assert events[-2].audio_end_ms == 300

    with websocket_server() as (url, calls):
        asyncio.run(run(url))
    assert len([call for call in calls if isinstance(call, dict)]) == 1


def test_recoverable_and_future_events(websocket_server):
    def handler(socket, start):
        socket.send(json.dumps(ready()))
        socket.send(json.dumps({"type": "new.event", "custom": 42}))
        socket.send(
            json.dumps(
                {
                    "type": "error",
                    "error": {"code": "recoverable", "message": "Try again."},
                    "fatal": False,
                    "session_id": "ses_test",
                    "sequence": 2,
                }
            )
        )
        socket.send(json.dumps({"type": "session.closed", "session_id": "ses_test", "sequence": 3}))

    with websocket_server(handler) as (url, calls), Tazo(base_url=url, api_key="test-key") as sdk:
        with sdk.decisions.stream(state="s", questions=QUESTIONS) as session:
            events = list(session)
    assert isinstance(events[0], UnknownEvent) and events[0].model_extra["custom"] == 42
    assert isinstance(events[1], ErrorEvent)


@pytest.mark.parametrize("async_mode", [False, True])
def test_fatal_handshake_errors_preserve_code(websocket_server, async_mode):
    def handler(socket, start):
        socket.send(
            json.dumps(
                {
                    "type": "error",
                    "fatal": True,
                    "error": {"code": "invalid_api_key", "message": "Invalid key."},
                }
            )
        )

    async def run(url):
        async with AsyncTazo(base_url=url, api_key="test-key") as sdk:
            async with sdk.decisions.stream(state="s", questions=QUESTIONS):
                pytest.fail("Handshake should fail")

    with websocket_server(handler) as (url, calls):
        with pytest.raises(StreamingError) as exc:
            if async_mode:
                asyncio.run(run(url))
            else:
                with Tazo(base_url=url, api_key="test-key") as sdk:
                    with sdk.decisions.stream(state="s", questions=QUESTIONS):
                        pytest.fail("Handshake should fail")
        assert exc.value.code == "invalid_api_key"
    assert len(calls) == 1


@pytest.mark.parametrize("async_mode", [False, True])
def test_disconnect_is_never_replayed(websocket_server, async_mode):
    def handler(socket, start):
        socket.send(json.dumps(ready()))
        socket.close(code=1011)

    async def run(url):
        async with AsyncTazo(base_url=url, api_key="test-key") as sdk:
            async with sdk.decisions.stream(state="s", questions=QUESTIONS) as session:
                await anext(session)

    with websocket_server(handler) as (url, calls):
        with pytest.raises(StreamingError) as exc:
            if async_mode:
                asyncio.run(run(url))
            else:
                with Tazo(base_url=url, api_key="test-key") as sdk:
                    with sdk.decisions.stream(state="s", questions=QUESTIONS) as session:
                        next(session)
        assert exc.value.code == "connection_lost" and exc.value.close_code == 1011
    assert len(calls) == 1


def test_async_receive_cancellation_closes_socket(websocket_server):
    async def run(url):
        async with AsyncTazo(base_url=url, api_key="test-key") as sdk:
            async with sdk.decisions.stream(state="s", questions=QUESTIONS) as session:
                receiver = asyncio.create_task(anext(session))
                await asyncio.sleep(0.01)
                with pytest.raises(Exception, match="one receiver"):
                    await anext(session)
                receiver.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await receiver
                assert session._state.phase == "closed"

    with websocket_server() as (url, calls):
        asyncio.run(run(url))


@pytest.mark.parametrize("async_mode", [False, True])
def test_event_timeout_is_bounded(websocket_server, async_mode):
    async def run(url):
        async with AsyncTazo(base_url=url, api_key="test-key") as sdk:
            async with sdk.decisions.stream(
                state="s", questions=QUESTIONS, event_timeout=0.05
            ) as session:
                await anext(session)

    with websocket_server() as (url, calls):
        with pytest.raises(StreamingError) as exc:
            if async_mode:
                asyncio.run(run(url))
            else:
                with Tazo(base_url=url, api_key="test-key") as sdk:
                    with sdk.decisions.stream(
                        state="s", questions=QUESTIONS, event_timeout=0.05
                    ) as session:
                        next(session)
        assert exc.value.code == "receive_timeout"


def test_protocol_errors_close_session(websocket_server):
    def handler(socket, start):
        socket.send(json.dumps(ready()))
        socket.send("not-json")

    with websocket_server(handler) as (url, calls), Tazo(base_url=url, api_key="test-key") as sdk:
        with sdk.decisions.stream(state="s", questions=QUESTIONS) as session:
            with pytest.raises(ProtocolError):
                next(session)
            assert session._state.phase == "closed"


def test_server_turn_limit_and_invalid_stream_configuration(websocket_server):
    def handler(socket, start):
        socket.send(json.dumps(ready(max_turn_audio_ms=100)))
        for _ in socket:
            pass

    with websocket_server(handler) as (url, calls), Tazo(base_url=url, api_key="test-key") as sdk:
        for interval in (0, 100, 240, 249, 30001, True, 1.5):
            with pytest.raises(InvalidRequestError):
                sdk.decisions.stream(
                    state="s", questions=QUESTIONS, evaluation_interval_ms=interval
                )
        with sdk.decisions.stream(state="s", questions=QUESTIONS) as session:
            session.send_audio(PCM)
            with pytest.raises(InvalidRequestError, match="max_turn"):
                session.send_audio(PCM)
    assert len(calls) == 1


@pytest.mark.parametrize("interval", [250, 510, 1000, 30000])
@pytest.mark.parametrize("async_mode", [False, True])
def test_valid_integer_interval_is_sent_unchanged(websocket_server, interval, async_mode):
    async def run(url):
        async with AsyncTazo(base_url=url, api_key="test-key") as sdk:
            async with sdk.decisions.stream(
                state="s", questions=QUESTIONS, evaluation_interval_ms=interval
            ) as session:
                await session.finish()
                assert [event.type async for event in session] == ["session.closed"]

    with websocket_server() as (url, calls):
        if async_mode:
            asyncio.run(run(url))
        else:
            with Tazo(base_url=url, api_key="test-key") as sdk:
                with sdk.decisions.stream(
                    state="s", questions=QUESTIONS, evaluation_interval_ms=interval
                ) as session:
                    session.finish()
                    assert [event.type for event in session] == ["session.closed"]
    assert calls[0]["evaluation_interval_ms"] == interval


@pytest.mark.parametrize("async_mode", [False, True])
def test_default_chunk_stream_allows_continuous_audio(websocket_server, async_mode):
    audio = []

    def handler(socket, start):
        assert start["audio_scope"] == "chunk"
        socket.send(json.dumps(ready(max_turn_audio_ms=None, audio_scope="chunk")))
        for message in socket:
            if isinstance(message, bytes):
                audio.append(message)
            else:
                assert json.loads(message)["type"] == "input.finish"
                socket.send(
                    json.dumps({"type": "session.closed", "session_id": "ses_test", "sequence": 2})
                )
                break

    async def run(url):
        async with AsyncTazo(base_url=url, api_key="test-key") as sdk:
            async with sdk.decisions.stream(state="s", questions=QUESTIONS) as session:
                assert session.ready.max_turn_audio_ms is None
                for _ in range(650):
                    await session.send_audio(PCM)
                await session.finish()
                assert [event.type async for event in session] == ["session.closed"]

    with websocket_server(handler) as (url, calls):
        if async_mode:
            asyncio.run(run(url))
        else:
            with Tazo(base_url=url, api_key="test-key") as sdk:
                with sdk.decisions.stream(state="s", questions=QUESTIONS) as session:
                    assert session.ready.max_turn_audio_ms is None
                    for _ in range(650):
                        session.send_audio(PCM)
                    session.finish()
                    assert [event.type for event in session] == ["session.closed"]
    assert audio == [PCM] * 650


def test_wav_helper_and_invalid_chunks(wav_file):
    assert list(wav_chunks(wav_file)) == [PCM] * 3
    assert b"".join(wav_chunks(wav_file, chunk_ms=200)) == PCM * 3
    with pytest.raises(InvalidRequestError):
        list(wav_chunks(wav_file, chunk_ms=201))


def test_explicit_sync_close_unblocks_receiver(websocket_server):
    with websocket_server() as (url, calls), Tazo(base_url=url, api_key="test-key") as sdk:
        with sdk.decisions.stream(state="s", questions=QUESTIONS) as session:
            results = []
            started = threading.Event()

            def receive():
                started.set()
                results.append(next(session, None))

            receiver = threading.Thread(target=receive)
            receiver.start()
            assert started.wait(timeout=1)
            session.close()
            receiver.join(timeout=3)
            assert not receiver.is_alive()
            assert results == [None]


def test_explicit_async_close_unblocks_receiver(websocket_server):
    async def run(url):
        async with AsyncTazo(base_url=url, api_key="test-key") as sdk:
            async with sdk.decisions.stream(state="s", questions=QUESTIONS) as session:
                receiver = asyncio.create_task(anext(session))
                await asyncio.sleep(0)
                await session.close()
                with pytest.raises(StopAsyncIteration):
                    await receiver

    with websocket_server() as (url, calls):
        asyncio.run(run(url))


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("scope", ["turn", "chunk"])
def test_numeric_duration_limit_still_applies_in_both_clients(websocket_server, async_mode, scope):
    def handler(socket, start):
        assert start["audio_scope"] == scope
        socket.send(json.dumps(ready(max_turn_audio_ms=100, audio_scope=scope)))
        for _ in socket:
            pass

    async def run(url):
        async with AsyncTazo(base_url=url, api_key="test-key") as sdk:
            async with sdk.decisions.stream(
                state="s", questions=QUESTIONS, audio_scope=scope
            ) as session:
                await session.send_audio(PCM)
                with pytest.raises(InvalidRequestError, match="max_turn_audio_ms"):
                    await session.send_audio(PCM)

    with websocket_server(handler) as (url, _):
        if async_mode:
            asyncio.run(run(url))
        else:
            with Tazo(base_url=url, api_key="test-key") as sdk:
                with sdk.decisions.stream(
                    state="s", questions=QUESTIONS, audio_scope=scope
                ) as session:
                    session.send_audio(PCM)
                    with pytest.raises(InvalidRequestError, match="max_turn_audio_ms"):
                        session.send_audio(PCM)
