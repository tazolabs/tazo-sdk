"""Ask two questions about one WAV with either client and uploads or streaming.

Set TAZO_API_KEY, then run:
    python examples/multiple_questions.py examples/weather.wav
    python examples/multiple_questions.py examples/weather.wav --async-client --mode stream
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from tazo import AsyncTazo, Tazo, wav_chunks
from tazo.types import DecisionEvent, DecisionResponse, ModelList, QuestionsInput

STATE = "Listen to the user's spoken request."
QUESTIONS: QuestionsInput = {
    "action": {
        "type": "choice",
        "instructions": "Which action should be taken?",
        "criteria": {
            "get_weather": "The speaker asks about the weather.",
            "NO_TOOL": "No weather tool is needed.",
        },
    },
    "asking_question": {
        "type": "noul",
        "instructions": "Is the speaker asking a question?",
    },
}


def require_capabilities(models: ModelList) -> None:
    """Check the actual target before submitting work during a regional rollout."""
    model = next((item for item in models.models if item.id == "tazo-large"), None)
    if model is None or not model.ready:
        raise RuntimeError("Tazo Large is not ready on this deployment.")
    if model.capabilities.max_questions < len(QUESTIONS):
        raise RuntimeError("This deployment has not enabled multiple questions yet.")
    if not {"choice", "noul"}.issubset(model.capabilities.question_types):
        raise RuntimeError("This deployment does not support both example question types.")


def stream_sync(client: Tazo, audio: Path) -> DecisionEvent:
    """Send audio concurrently with the single event receiver, then finish cleanly."""
    final: DecisionEvent | None = None
    with (
        client.decisions.stream(state=STATE, questions=QUESTIONS, audio_scope="turn") as session,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):

        def send() -> None:
            try:
                limit = session.ready.max_audio_message_bytes & ~1
                for chunk in wav_chunks(audio):
                    for offset in range(0, len(chunk), limit):
                        part = chunk[offset : offset + limit]
                        time.sleep(len(part) / 32000)
                        session.send_audio(part)
                session.commit()
            except BaseException:
                session.close()
                raise

        sender = pool.submit(send)
        try:
            for event in session:
                if isinstance(event, DecisionEvent) and event.type == "decision.finalized":
                    final = event
                    session.finish()
        finally:
            session.close()
            sender.result()
    if final is None:
        raise RuntimeError("The stream closed without a finalized decision.")
    return final


async def stream_async(client: AsyncTazo, audio: Path) -> DecisionEvent:
    """Ask the same question map with native async sending and receiving."""
    final: DecisionEvent | None = None
    async with client.decisions.stream(
        state=STATE, questions=QUESTIONS, audio_scope="turn"
    ) as session:

        async def send() -> None:
            limit = session.ready.max_audio_message_bytes & ~1
            for chunk in wav_chunks(audio):
                for offset in range(0, len(chunk), limit):
                    part = chunk[offset : offset + limit]
                    await asyncio.sleep(len(part) / 32000)
                    await session.send_audio(part)
            await session.commit()

        async def receive() -> None:
            nonlocal final
            async for event in session:
                if isinstance(event, DecisionEvent) and event.type == "decision.finalized":
                    final = event
                    await session.finish()

        async with asyncio.TaskGroup() as group:
            group.create_task(send())
            group.create_task(receive())
    if final is None:
        raise RuntimeError("The stream closed without a finalized decision.")
    return final


def run_sync(client: Tazo, audio: Path, mode: str) -> DecisionResponse | DecisionEvent:
    require_capabilities(client.models.list())
    if mode == "stream":
        return stream_sync(client, audio)
    return client.decisions.create(audio=audio, state=STATE, questions=QUESTIONS)


async def run_async(client: AsyncTazo, audio: Path, mode: str) -> DecisionResponse | DecisionEvent:
    require_capabilities(await client.models.list())
    if mode == "stream":
        return await stream_async(client, audio)
    return await client.decisions.create(audio=audio, state=STATE, questions=QUESTIONS)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("--mode", choices=("upload", "stream"), default="upload")
    parser.add_argument("--async-client", action="store_true")
    parser.add_argument("--base-url", default="https://api.tazolabs.com")
    args = parser.parse_args()

    async def asynchronous() -> DecisionResponse | DecisionEvent:
        async with AsyncTazo(base_url=args.base_url) as client:
            return await run_async(client, args.audio, args.mode)

    if args.async_client:
        result = asyncio.run(asynchronous())
    else:
        with Tazo(base_url=args.base_url) as client:
            result = run_sync(client, args.audio, args.mode)
    print(
        json.dumps({key: answer.model_dump() for key, answer in result.answers.items()}, indent=2)
    )


if __name__ == "__main__":
    main()
