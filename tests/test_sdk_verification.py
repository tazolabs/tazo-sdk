"""The verification example uses real HTTP/WebSockets and a standalone public-API double."""

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import threading
import wave
from pathlib import Path

import pytest
from conftest import PCM, ready

from tazo import AsyncTazo, Tazo
from tazo.streaming import AsyncStreamingSession, StreamingSession
from tazo.types import SessionReady

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def verification(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "sdk_verification_example", ROOT / "examples/verify.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def recording(tmp_path):
    path = tmp_path / "sample.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x01\x00" * 3200)
    return path


def test_verification_chunks_accept_nullable_limit_and_preserve_numeric_limit(verification):
    audio = verification.Audio(filename="long.wav", wav=b"", pcm=PCM * 650)
    assert audio.duration_ms == 65000
    assert (
        list(audio.chunks(SessionReady.model_validate(ready(max_turn_audio_ms=None))))
        == [PCM] * 650
    )
    with pytest.raises(ValueError, match="turn limit"):
        list(audio.chunks(SessionReady.model_validate(ready(max_turn_audio_ms=30000))))


@pytest.mark.parametrize("expected,exit_code", [(None, 0), ("NO_TOOL", 0), ("get_weather", 1)])
def test_all_four_modes_for_both_models_and_semantic_failure(
    fake_api,
    verification,
    recording,
    tmp_path,
    monkeypatch,
    capsys,
    expected,
    exit_code,
):
    api = fake_api
    report = tmp_path / "report.json"
    args = [
        "verify.py",
        "--audio",
        str(recording),
        "--base-url",
        api.base_url,
        "--timeout",
        "5",
        "--output",
        str(report),
    ]
    if expected is not None:
        args.extend(["--expected-choice", expected])
    monkeypatch.setenv("TAZO_API_KEY", "valid-token")
    monkeypatch.setattr(sys, "argv", args)
    assert verification.main() == exit_code
    result = json.loads(report.read_text())
    assert result["total"] == 8
    assert result["passed"] == (8 if exit_code == 0 else 0)
    assert {(item["model"], item["mode"]) for item in result["results"]} == {
        (model, mode)
        for model in ("tazo-large", "tazo-small")
        for mode in ("sync-upload", "sync-stream", "async-upload", "async-stream")
    }
    if exit_code == 0:
        assert all(
            item["decision"]["answers"]["action"]["choice"] == "NO_TOOL"
            for item in result["results"]
        )
    else:
        assert all(
            "Expected get_weather, received NO_TOOL" in item["error"] for item in result["results"]
        )
    assert "valid-token" not in report.read_text()
    assert len(api.uploads) == 4
    assert sum(path == "/v1/decisions/stream" for _, path in api.calls) == 4
    assert "Summary:" in capsys.readouterr().out
    assert not any(t.name == "tazo-audio-sender" for t in threading.enumerate())


def test_sync_sender_failure_reaches_caller_and_stops_thread(
    fake_api, verification, recording, monkeypatch
):
    api = fake_api

    def fail_send(self, audio):
        raise RuntimeError("producer_failed")

    monkeypatch.setattr(StreamingSession, "send_audio", fail_send)
    with (
        Tazo(api_key="valid-token", base_url=api.base_url) as client,
        pytest.raises(RuntimeError, match="producer_failed"),
    ):
        verification.sync_stream(client, verification.load_audio(recording), "tazo-large", 2)
    assert not any(t.name == "tazo-audio-sender" for t in threading.enumerate())


def test_async_receiver_failure_cancels_producer(fake_api, verification, recording, monkeypatch):
    api = fake_api

    async def fail_receive(self):
        raise RuntimeError("receiver_failed")

    monkeypatch.setattr(AsyncStreamingSession, "__anext__", fail_receive)

    async def run():
        async with AsyncTazo(api_key="valid-token", base_url=api.base_url) as client:
            with pytest.raises(ExceptionGroup) as failure:
                await verification.async_stream(
                    client, verification.load_audio(recording), "tazo-large", 2
                )
            assert "receiver_failed" in verification.error_message(failure.value)
        pending = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done()
        ]
        assert not pending

    asyncio.run(run())


def test_cli_runs_with_installed_sdk(fake_api, recording, tmp_path):
    api = fake_api
    report = tmp_path / "cli-report.json"
    # An isolated PyPI environment can be selected when validating a published release.
    python = os.environ.get("SDK_VERIFICATION_PYTHON", sys.executable)
    result = subprocess.run(
        [
            python,
            str(ROOT / "examples/verify.py"),
            "--audio",
            str(recording),
            "--base-url",
            api.base_url,
            "--expected-choice",
            "NO_TOOL",
            "--output",
            str(report),
            "--timeout",
            "5",
        ],
        env={**os.environ, "TAZO_API_KEY": "valid-token"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Summary: 8/8 passed" in result.stdout
    assert json.loads(report.read_text())["passed"] == 8
