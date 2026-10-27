import asyncio
import importlib.util
import json
import re
from pathlib import Path

import httpx
import yaml
from conftest import decision

from tazo import AsyncTazo

ROOT = Path(__file__).resolve().parents[1]


def test_python_documentation_and_endpoint_samples_execute(
    monkeypatch, tmp_path, wav_file, websocket_server
):
    from test_http import multipart

    monkeypatch.setenv("TAZO_API_KEY", "test-key")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "request.wav").write_bytes(wav_file.read_bytes())
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"models": []})
        assert request.url.path == "/v1/decisions"
        payload = json.loads(multipart(request)["request"][0])
        answers = {}
        for key, question in payload["questions"].items():
            if question["type"] == "noul":
                answers[key] = {"type": "noul", "noul": 0.9}
            else:
                choice = next(iter(question["criteria"]))
                answers[key] = {
                    "type": "choice",
                    "choice": choice,
                    "confidence": 1.0,
                    "probabilities": {k: float(k == choice) for k in question["criteria"]},
                }
        return httpx.Response(200, json=decision(answers=answers))

    sync_client, async_client = httpx.Client, httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: sync_client(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: async_client(transport=httpx.MockTransport(handler))
    )
    with websocket_server() as (url, stream_calls):
        monkeypatch.setattr(
            "tazo.resources._stream_url",
            lambda base: url.replace("http:", "ws:") + "/v1/decisions/stream",
        )
        docs = list((ROOT / "docs/mintlify").glob("*.mdx"))
        docs.append(ROOT / "README.md")
        count = 0
        for file in docs:
            namespace = {}
            for code in re.findall(r"```python[^\n]*\n(.*?)```", file.read_text(), flags=re.S):
                exec(compile(code, str(file), "exec"), namespace)
                count += 1
        spec = yaml.safe_load((ROOT / "api/openapi.yaml").read_text())
        for operation in (
            spec["paths"]["/v1/decisions"]["post"],
            spec["paths"]["/v1/models"]["get"],
            spec["paths"]["/healthz"]["get"],
        ):
            for sample in operation["x-codeSamples"]:
                exec(compile(sample["source"], "endpoint sample", "exec"), {})
                count += 1
    assert count >= 15
    assert calls and stream_calls


def test_streaming_example_completes_and_propagates_sender_failure(wav_file, websocket_server):
    path = ROOT / "examples/stream.py"
    spec = importlib.util.spec_from_file_location("sdk_stream_example", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    async def run(url):
        async with AsyncTazo(base_url=url, api_key="test-key") as client:
            await asyncio.wait_for(module.stream_recording(client, wav_file, module.QUESTIONS), 3)
            try:
                await asyncio.wait_for(
                    module.stream_recording(client, Path("missing.wav"), module.QUESTIONS), 3
                )
            except ExceptionGroup as errors:
                assert any(isinstance(error, FileNotFoundError) for error in errors.exceptions)
            else:
                raise AssertionError("Producer failure was swallowed")

    with websocket_server() as (url, calls):
        asyncio.run(run(url))


def test_explicit_staging_smoke_uses_only_public_apis(wav_file, websocket_server):
    from test_http import multipart

    spec = importlib.util.spec_from_file_location("sdk_smoke_example", ROOT / "examples/smoke.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "id": name,
                            "display_name": name,
                            "description": "Model",
                            "ready": True,
                            "contract": "typed-decisions-v1",
                            "capabilities": {"question_types": ["choice"], "max_questions": 1},
                        }
                        for name in ("tazo-large", "tazo-small")
                    ]
                },
            )
        assert request.url.path == "/v1/decisions"
        body = json.loads(multipart(request)["request"][0])
        return httpx.Response(
            200,
            headers={"X-Tazo-Request-Id": request.headers["Idempotency-Key"]},
            json=decision(
                model=body["model"],
                answers={
                    "action": {
                        "type": "choice",
                        "choice": "NO_TOOL",
                        "confidence": 0.9,
                        "probabilities": {"NO_TOOL": 0.9, "get_weather": 0.1},
                    }
                },
            ),
        )

    async def run(url):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
            async with AsyncTazo(api_key="test-key", base_url=url, http_client=transport) as client:
                await asyncio.wait_for(module.run_smoke(client, wav_file), 3)

    with websocket_server() as (url, sockets):
        asyncio.run(run(url))
    uploads = [r for r in calls if r.method == "POST"]
    assert len(uploads) == 4
    assert uploads[0].headers["Idempotency-Key"] == uploads[1].headers["Idempotency-Key"]
    assert uploads[2].headers["Idempotency-Key"] == uploads[3].headers["Idempotency-Key"]
    assert uploads[0].headers["Idempotency-Key"] != uploads[2].headers["Idempotency-Key"]
    assert len([start for start in sockets if isinstance(start, dict)]) == 2
