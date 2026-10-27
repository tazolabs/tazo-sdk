import asyncio
import json
import threading

import pytest
from conftest import PCM, QUESTIONS, ready

from tazo import AsyncTazo, InvalidRequestError, ProtocolError, StreamingError, Tazo, wav_chunks
from tazo.types import ErrorEvent, UnknownEvent


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
        for interval in (0, 99, 30001, True, 1.5):
            with pytest.raises(InvalidRequestError):
                sdk.decisions.stream(
                    state="s", questions=QUESTIONS, evaluation_interval_ms=interval
                )
        with sdk.decisions.stream(state="s", questions=QUESTIONS) as session:
            session.send_audio(PCM)
            with pytest.raises(InvalidRequestError, match="max_turn"):
                session.send_audio(PCM)
    assert len(calls) == 1


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
