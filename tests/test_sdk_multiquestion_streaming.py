import asyncio
import json

import pytest
from conftest import QUESTIONS, ready

from tazo import AsyncTazo, StreamingError, Tazo
from tazo.streaming import _State, decision_request


def questions(count):
    return {f"q{i}": QUESTIONS["action"] for i in range(count)}


def test_sdk_preserves_all_questions_on_start_and_update():
    state = _State(decision_request("tazo-large", "s", questions(10)), "turn", None, 30)
    assert set(json.loads(state.start("key"))["questions"]) == set(questions(10))
    state.initialize(json.dumps(ready()))
    updated = {"urgent": {"type": "noul", "instructions": "Urgent?"}, **questions(9)}
    payload = json.loads(state.control("session.update", state="updated", questions=updated))
    assert set(payload["questions"]) == set(updated)


@pytest.mark.parametrize("client", [Tazo, AsyncTazo])
def test_both_clients_accept_ten_questions_without_a_network_request(client):
    async def check_async():
        async with AsyncTazo(api_key="test-key") as sdk:
            session = sdk.decisions.stream(state="s", questions=questions(10))
            assert len(session._state.request.questions) == 10

    if client is AsyncTazo:
        asyncio.run(check_async())
    else:
        with Tazo(api_key="test-key") as sdk:
            session = sdk.decisions.stream(state="s", questions=questions(10))
            assert len(session._state.request.questions) == 10


def test_server_remains_authoritative_for_model_specific_question_limit():
    state = _State(decision_request("tazo-small", "s", questions(2)), "turn", None, 30)
    with pytest.raises(StreamingError) as error:
        state.initialize(
            json.dumps(
                {
                    "type": "error",
                    "fatal": True,
                    "error": {
                        "code": "invalid_questions",
                        "message": "Provide at most 1 questions.",
                    },
                }
            )
        )
    assert error.value.code == "invalid_questions"
