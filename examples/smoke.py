"""Explicit live API smoke test of all four public APIs; never runs automatically.

Use: TAZO_API_KEY=... python smoke.py --base-url https://api.tazolabs.com --audio short.wav
The recording must be a mono PCM16 WAV (8-48 kHz) and shorter than the advertised turn limit.
This command submits real decision work. Use a dedicated test account and recording.
"""

from __future__ import annotations

import argparse
import asyncio
import wave
from pathlib import Path
from uuid import uuid4

from tazo import AsyncTazo, wav_chunks, wav_sample_rate
from tazo.types import QuestionsInput

QUESTIONS: QuestionsInput = {
    "action": {
        "type": "choice",
        "instructions": "Which action should be taken?",
        "criteria": {"get_weather": "Get weather.", "NO_TOOL": "No tool needed."},
    }
}


async def run_smoke(client: AsyncTazo, audio: Path) -> None:
    """Check health, both model uploads with keyed replay, and streamed completion."""
    if (await client.health.check()).status != "ok":
        raise RuntimeError("The target is not ready.")
    models = {model.id: model for model in (await client.models.list()).models}
    with wave.open(str(audio), "rb") as handle:
        duration_ms = handle.getnframes() * 1000 / handle.getframerate()
    # Validate format and truncation before any decision is admitted.
    for _ in wav_chunks(audio):
        pass
    for model_id in ("tazo-large", "tazo-small"):
        if model_id not in models or not models[model_id].ready:
            raise RuntimeError(f"{model_id} is not ready.")
        key = str(uuid4())
        first = await client.decisions.create(
            audio=audio,
            model=model_id,
            state="Choose an action.",
            questions=QUESTIONS,
            idempotency_key=key,
        )
        replay = await client.decisions.create(
            audio=audio,
            model=model_id,
            state="Choose an action.",
            questions=QUESTIONS,
            idempotency_key=key,
        )
        if not first.request_id or first.request_id != replay.request_id or first.id != replay.id:
            raise RuntimeError(
                "The target did not preserve the durable operation across keyed replay."
            )
        async with client.decisions.stream(
            state="Choose an action.",
            questions=QUESTIONS,
            model=model_id,
            sample_rate_hz=wav_sample_rate(audio),
        ) as session:
            if (
                session.ready.max_turn_audio_ms is not None
                and duration_ms > session.ready.max_turn_audio_ms
            ):
                raise RuntimeError("Recording exceeds the streaming turn limit; use a shorter WAV.")

            async def send() -> None:
                for chunk in wav_chunks(audio):
                    await asyncio.sleep(len(chunk) / (wav_sample_rate(audio) * 2))
                    await session.send_audio(chunk)
                await session.commit()

            async def receive() -> None:
                final = False
                async for event in session:
                    if event.type == "decision.finalized":
                        final = True
                        await session.finish()
                if not final:
                    raise RuntimeError("Session closed without a final decision.")

            async with asyncio.TaskGroup() as group:
                group.create_task(send())
                group.create_task(receive())
        print(f"{model_id}: upload, keyed replay, and streaming passed ({first.request_id})")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Explicit API origin without /v1")
    parser.add_argument("--audio", type=Path, required=True)
    args = parser.parse_args()
    async with AsyncTazo(base_url=args.base_url) as client:
        await run_smoke(client, args.audio)


if __name__ == "__main__":
    asyncio.run(main())
