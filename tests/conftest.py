from __future__ import annotations

import json
import threading
import wave
from contextlib import contextmanager

import pytest
from fake_api import fake_api  # noqa: F401 - register the standalone HTTP/WebSocket fixture
from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve

QUESTIONS = {
    "action": {
        "type": "choice",
        "instructions": "Choose.",
        "criteria": {"NO_TOOL": "Wait.", "SEARCH": "Search."},
    }
}
PCM = b"\x01\x00" * 1600


def decision(**overrides):
    return {
        "id": "dec_test",
        "model": "tazo-large",
        "answers": {
            "action": {
                "type": "choice",
                "choice": "SEARCH",
                "probabilities": {"NO_TOOL": 0.1, "SEARCH": 0.9},
                "confidence": 0.9,
            }
        },
        "usage": {"audio_seconds": 0.1},
        "elapsed_ms": 2,
        **overrides,
    }


def ready(**overrides):
    return {
        "type": "session.ready",
        "session_id": "ses_test",
        "sequence": 1,
        "model": "tazo-large",
        "config_revision": 1,
        "input_audio": {"encoding": "pcm_s16le", "sample_rate_hz": 16000, "channels": 1},
        "evaluation_interval_ms": 100,
        "max_turn_audio_ms": 30000,
        "max_audio_message_bytes": 6400,
        "audio_scope": "turn",
        **overrides,
    }


@pytest.fixture
def wav_file(tmp_path):
    path = tmp_path / "audio.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(PCM * 3)
    return path


@pytest.fixture
def websocket_server():
    @contextmanager
    def start(custom=None):
        calls = []
        sockets = []

        def handler(socket):
            sockets.append(socket)
            try:
                payload = json.loads(socket.recv())
                calls.append(payload)
                assert payload["authorization"] == "Bearer test-key"
                if custom is not None:
                    custom(socket, payload)
                    return
                sequence = 1
                revision = 1
                turn = 1
                audio = bytearray()
                scope = payload["audio_scope"]
                socket.send(
                    json.dumps(
                        ready(
                            audio_scope=scope, max_turn_audio_ms=None if scope == "chunk" else 30000
                        )
                    )
                )

                def emit(body):
                    nonlocal sequence
                    sequence += 1
                    socket.send(
                        json.dumps({**body, "session_id": "ses_test", "sequence": sequence})
                    )

                def emit_decision(kind):
                    emit(
                        {
                            **decision(),
                            "type": kind,
                            "turn_id": f"turn_{turn}",
                            "config_revision": revision,
                            "audio_start_ms": 0,
                            "audio_end_ms": len(audio) / 32,
                        }
                    )

                for message in socket:
                    calls.append(message)
                    if isinstance(message, bytes):
                        audio.extend(message)
                        emit_decision("decision.updated")
                        continue
                    event = json.loads(message)
                    if event["type"] == "input.commit":
                        emit_decision("decision.finalized")
                        audio.clear()
                        turn += 1
                    elif event["type"] == "session.update":
                        revision += 1
                        emit({"type": "session.updated", "config_revision": revision})
                    elif event["type"] == "input.finish":
                        if audio:
                            emit_decision("decision.finalized")
                        emit({"type": "session.closed"})
                        socket.close()
                        return
            except ConnectionClosed:
                pass

        server = serve(handler, "127.0.0.1", 0, compression=None, close_timeout=1)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.socket.getsockname()[1]}", calls
        finally:
            for socket in sockets:
                socket.close()
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()

    return start
