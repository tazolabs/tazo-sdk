"""Stream a short PCM16 WAV: python stream.py request.wav (TAZO_API_KEY required)."""

import argparse
import asyncio
from pathlib import Path

from tazo import AsyncTazo, wav_chunks
from tazo.types import QuestionsInput

QUESTIONS: QuestionsInput = {
    "action": {
        "type": "choice",
        "instructions": "Which action should be taken?",
        "criteria": {"get_weather": "Get weather.", "NO_TOOL": "No tool needed."},
    }
}


async def stream_recording(client: AsyncTazo, path: Path, questions: QuestionsInput) -> None:
    """Pace WAV samples and propagate failures from either producer or receiver."""
    async with client.decisions.stream(state="Choose an action.", questions=questions) as session:

        async def send() -> None:
            for chunk in wav_chunks(path):
                await asyncio.sleep(len(chunk) / 32000)
                await session.send_audio(chunk)
            await session.commit()

        async def receive() -> None:
            async for event in session:
                print(event.model_dump_json())
                if event.type == "decision.finalized":
                    await session.finish()

        async with asyncio.TaskGroup() as group:
            group.create_task(send())
            group.create_task(receive())


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("--base-url", default="https://api.tazolabs.com")
    args = parser.parse_args()
    async with AsyncTazo(base_url=args.base_url) as client:
        await stream_recording(client, args.audio, QUESTIONS)


if __name__ == "__main__":
    asyncio.run(main())
