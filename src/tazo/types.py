"""Public wire types. Responses preserve additive server fields."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal, NotRequired, TypedDict

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

StructuredValue = str | dict[str, Any] | list[str]
Criterion = StructuredValue | None


class ChoiceQuestionParam(TypedDict):
    """Dictionary input with editor completion for a choice question."""

    type: Literal["choice"]
    instructions: StructuredValue
    criteria: dict[str, Criterion]


class NoulQuestionParam(TypedDict):
    """Dictionary input for a yes/no probability question."""

    type: Literal["noul"]
    instructions: StructuredValue
    criteria: NotRequired[dict[str, Criterion] | None]


class RequestModel(BaseModel):
    """Strict input model; misspelled fields are rejected locally."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ChoiceQuestion(RequestModel):
    """Select an ID from two or more criteria; model-specific limits still apply."""

    type: Literal["choice"] = "choice"
    instructions: StructuredValue
    criteria: dict[str, Criterion] = Field(min_length=2, max_length=255)

    @field_validator("criteria")
    @classmethod
    def nonblank_choices(cls, value: dict[str, Criterion]) -> dict[str, Criterion]:
        if any(not key.strip() for key in value):
            raise ValueError("Choice IDs must not be blank.")
        return value


class NoulQuestion(RequestModel):
    """Ask for the probability that a statement is true, where supported."""

    type: Literal["noul"] = "noul"
    instructions: StructuredValue
    criteria: dict[str, Criterion] | None = None

    @field_validator("criteria")
    @classmethod
    def boolean_criteria(cls, value: dict[str, Criterion] | None) -> dict[str, Criterion] | None:
        if value is not None and set(value) != {"true", "false"}:
            raise ValueError("Noul criteria must contain true and false.")
        return value


Question = Annotated[ChoiceQuestion | NoulQuestion, Field(discriminator="type")]
QuestionsInput = Mapping[
    str, ChoiceQuestion | NoulQuestion | ChoiceQuestionParam | NoulQuestionParam
]


class DecisionRequest(RequestModel):
    """The JSON part of an uploaded decision."""

    model: str = Field(default="tazo-large", min_length=1)
    state: StructuredValue
    questions: dict[str, Question] = Field(min_length=1)

    @field_validator("model")
    @classmethod
    def nonblank_model(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Model must not be blank.")
        return value.strip()

    @field_validator("questions")
    @classmethod
    def nonblank_questions(cls, value: dict[str, Question]) -> dict[str, Question]:
        if any(not key.strip() for key in value):
            raise ValueError("Question IDs must not be blank.")
        return value


class ResponseModel(BaseModel):
    """Forward-compatible response base; extra fields remain accessible."""

    model_config = ConfigDict(extra="allow", allow_inf_nan=False)


class ChoiceAnswer(ResponseModel):
    """Selected choice, full probability distribution, and confidence."""

    type: Literal["choice"]
    choice: str
    probabilities: dict[str, float]
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def valid_distribution(self) -> ChoiceAnswer:
        values = self.probabilities.values()
        if (
            self.choice not in self.probabilities
            or any(not 0 <= value <= 1 for value in values)
            or abs(sum(values) - 1) > 0.001
            or abs(self.confidence - self.probabilities[self.choice]) > 1e-6
        ):
            raise ValueError("Invalid choice probabilities or confidence.")
        return self


class NoulAnswer(ResponseModel):
    """Probability that the requested statement is true."""

    type: Literal["noul"]
    noul: float = Field(ge=0, le=1)


Answer = Annotated[ChoiceAnswer | NoulAnswer, Field(discriminator="type")]


class Usage(ResponseModel):
    """Audio evaluated by this result; cumulative streaming windows overlap."""

    audio_seconds: float = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)


class DecisionResponse(ResponseModel):
    """Decision result with SDK metadata from headers and the submitted operation."""

    id: str = Field(pattern=r"^dec_[A-Za-z0-9_-]+$")
    model: str
    answers: dict[str, Answer] = Field(min_length=1)
    usage: Usage
    elapsed_ms: float = Field(ge=0)
    request_id: str | None = Field(default=None, exclude=True)
    idempotency_key: str | None = Field(default=None, exclude=True)


class ModelCapabilities(ResponseModel):
    """Capabilities advertised by a model, including future question types."""

    question_types: list[str]
    max_questions: int = Field(ge=1)
    max_choice_options: int | None = None
    requires_no_tool_choice: bool = False
    max_audio_seconds: float | None = None
    required_sample_rate_hz: int | None = None


class Model(ResponseModel):
    """A public model and its current readiness."""

    id: str
    display_name: str
    description: str
    contract: str
    capabilities: ModelCapabilities
    ready: bool


class ModelList(ResponseModel):
    """Response from model discovery."""

    models: list[Model]


class HealthStatus(ResponseModel):
    """Expected health result, including HTTP 503 when unavailable."""

    status: Literal["ok", "unavailable"]


class AudioFormat(ResponseModel):
    """Streaming audio format negotiated by the server."""

    encoding: Literal["pcm_s16le"] = "pcm_s16le"
    sample_rate_hz: Literal[16000] = 16000
    channels: Literal[1] = 1


class SessionEvent(ResponseModel):
    """An event with monotonically increasing connection sequence numbers."""

    type: str
    session_id: str
    sequence: int = Field(ge=1)


class SessionReady(SessionEvent):
    """Configuration accepted; available as the session's ``ready`` attribute."""

    type: Literal["session.ready"]
    model: str
    config_revision: int = Field(ge=1)
    input_audio: AudioFormat
    evaluation_interval_ms: int = Field(gt=0)
    max_turn_audio_ms: int | None = Field(gt=0)
    max_audio_message_bytes: int = Field(gt=0)
    audio_scope: Literal["turn", "chunk"]


class SessionUpdated(SessionEvent):
    """Replacement context and question accepted between turns."""

    type: Literal["session.updated"]
    config_revision: int = Field(ge=1)


class DecisionEvent(DecisionResponse):
    """Provisional or final decision for the reported audio window."""

    type: Literal["decision.updated", "decision.finalized"]
    session_id: str
    sequence: int = Field(ge=1)
    turn_id: str
    config_revision: int = Field(ge=1)
    audio_start_ms: float = Field(default=0, ge=0)
    audio_end_ms: float = Field(ge=0)


class SessionClosed(SessionEvent):
    """The server finished the session normally."""

    type: Literal["session.closed"]


class StreamErrorDetail(ResponseModel):
    """Machine-readable streaming failure."""

    code: str
    message: str


class ErrorEvent(ResponseModel):
    """A recoverable error event; fatal errors become StreamingError exceptions."""

    type: Literal["error"]
    error: StreamErrorDetail
    fatal: bool
    session_id: str | None = None
    sequence: int | None = None


class UnknownEvent(ResponseModel):
    """A future event type. Its additional fields are preserved."""

    type: str


StreamEvent = SessionUpdated | DecisionEvent | SessionClosed | ErrorEvent | UnknownEvent
