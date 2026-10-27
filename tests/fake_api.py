"""A local public-API double; no Tazo credentials or backend code are needed."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass, field
from uuid import uuid4

import pytest
from aiohttp import WSMsgType, web

TEST_KEY = "valid-token"


@dataclass
class FakeAPI:
    base_url: str = ""
    health: str = "ok"
    choice: str = "NO_TOOL"
    ready: bool = True
    stream_failure: str | None = None
    upload_error: tuple[int, str] | None = None
    max_message_bytes: int = 998  # Exercise chunk adaptation and even byte boundaries.
    max_turn_ms: int = 30000
    calls: list[tuple[str, str]] = field(default_factory=list)
    audio_messages: list[bytes] = field(default_factory=list)
    uploads: list[bytes] = field(default_factory=list)

    def decision(self, model):
        return {
            "id": f"dec_{uuid4().hex}",
            "model": model,
            "answers": {
                "action": {
                    "type": "choice",
                    "choice": self.choice,
                    "probabilities": {
                        "get_weather": 0.9 if self.choice == "get_weather" else 0.1,
                        "NO_TOOL": 0.9 if self.choice == "NO_TOOL" else 0.1,
                    },
                    "confidence": 0.9,
                }
            },
            "usage": {"audio_seconds": 1.0},
            "elapsed_ms": 5,
        }

    async def handle(self, request):
        self.calls.append((request.method, request.path))
        if request.path == "/healthz":
            return web.json_response(
                {"status": self.health}, status=200 if self.health == "ok" else 503
            )
        if request.path == "/v1/decisions/stream":
            return await self.stream(request)
        if request.headers.get("Authorization") != f"Bearer {TEST_KEY}":
            return web.json_response(
                {"error": {"code": "unauthorized", "message": "Invalid credential"}}, status=401
            )
        if request.path == "/v1/models":
            return web.json_response(
                {
                    "models": [
                        {
                            "id": name,
                            "display_name": name,
                            "description": "Local test double",
                            "contract": "v1",
                            "ready": self.ready,
                            "capabilities": {
                                "question_types": ["choice"],
                                "max_questions": 1,
                                "max_choice_options": 2,
                                "requires_no_tool_choice": True,
                                "max_audio_seconds": 30,
                                "required_sample_rate_hz": 16000,
                            },
                        }
                        for name in ("tazo-large", "tazo-small")
                    ]
                }
            )
        if request.path == "/v1/decisions" and request.method == "POST":
            if self.upload_error:
                status, code = self.upload_error
                return web.json_response(
                    {"error": {"code": code, "message": f"Failed request {TEST_KEY}"}},
                    status=status,
                    headers={"Retry-After": "0", "X-Request-ID": "req_local"},
                )
            parts = await request.post()
            payload = json.loads(parts["request"])
            self.uploads.append(parts["audio"].file.read())
            assert request.headers.get("Idempotency-Key")
            return web.json_response(
                self.decision(payload["model"]), headers={"X-Request-ID": "req_local"}
            )
        return web.Response(status=404)

    async def stream(self, request):
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        start = await socket.receive_json()
        if start["authorization"] != f"Bearer {TEST_KEY}":
            await socket.send_json(
                {
                    "type": "error",
                    "fatal": True,
                    "error": {"code": "unauthorized", "message": "Bad key"},
                }
            )
            await socket.close()
            return socket
        assert start["type"] == "session.start"
        assert len(start["questions"]) == 1
        session_id = f"sess_{uuid4().hex}"
        await socket.send_json(
            {
                "type": "session.ready",
                "session_id": session_id,
                "sequence": 1,
                "model": start["model"],
                "config_revision": 1,
                "input_audio": {"encoding": "pcm_s16le", "sample_rate_hz": 16000, "channels": 1},
                "evaluation_interval_ms": 1000,
                "max_turn_audio_ms": self.max_turn_ms,
                "max_audio_message_bytes": self.max_message_bytes,
                "audio_scope": start["audio_scope"],
            }
        )
        received = 0
        async for message in socket:
            if message.type == WSMsgType.BINARY:
                self.audio_messages.append(message.data)
                received += len(message.data)
            elif message.type == WSMsgType.TEXT:
                control = json.loads(message.data)
                if control["type"] == "input.commit":
                    if self.stream_failure == "disconnect":
                        await socket.close()
                        break
                    if self.stream_failure == "missing-final":
                        await socket.send_json(
                            {"type": "session.closed", "session_id": session_id, "sequence": 2}
                        )
                        await socket.close()
                        break
                    await socket.send_json(
                        {
                            **self.decision(start["model"]),
                            "type": "decision.finalized",
                            "session_id": session_id,
                            "sequence": 2,
                            "turn_id": "turn_local",
                            "config_revision": 1,
                            "audio_start_ms": 0,
                            "audio_end_ms": received / 32,
                        }
                    )
                elif control["type"] == "input.finish":
                    await socket.send_json(
                        {"type": "session.closed", "session_id": session_id, "sequence": 3}
                    )
                    await socket.close()
                    break
        return socket


@pytest.fixture
def fake_api():
    api = FakeAPI()
    started = threading.Event()
    loop = asyncio.new_event_loop()
    runner = None

    async def start():
        nonlocal runner
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", api.handle)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        api.base_url = f"http://127.0.0.1:{port}"
        started.set()

    def serve():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(start())
        loop.run_forever()
        loop.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    assert started.wait(timeout=5), "Local fake API failed to start"
    try:
        yield api
    finally:
        asyncio.run_coroutine_threadsafe(runner.cleanup(), loop).result(timeout=5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        assert not thread.is_alive()
