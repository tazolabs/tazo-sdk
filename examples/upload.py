"""Run with TAZO_API_KEY set: python upload.py request.wav."""

import argparse
from pathlib import Path

from tazo import Tazo
from tazo.types import QuestionsInput

QUESTIONS: QuestionsInput = {
    "action": {
        "type": "choice",
        "instructions": "Which action should be taken?",
        "criteria": {"get_weather": "Get weather.", "NO_TOOL": "No tool needed."},
    }
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("--base-url", default="https://api.tazolabs.com")
    parser.add_argument(
        "--idempotency-key", help="Retained key for recovering an earlier operation"
    )
    args = parser.parse_args()
    with Tazo(base_url=args.base_url) as client:
        result = client.decisions.create(
            audio=args.audio,
            state="Choose an action.",
            questions=QUESTIONS,
            idempotency_key=args.idempotency_key,
        )
        print(result.model_dump_json(indent=2))
        print("Request:", result.request_id, "Operation:", result.idempotency_key)


if __name__ == "__main__":
    main()
