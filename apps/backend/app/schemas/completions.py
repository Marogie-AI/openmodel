from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.chat import UsageOut


class CompletionRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str
    prompt: str | Annotated[list[str], Field(min_length=1)]
    stream: bool = False
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None


class CompletionChoice(BaseModel):
    index: int = 0
    text: str
    finish_reason: str | None = None


class Completion(BaseModel):
    """Both the sync response and a stream chunk: OpenAI uses one shape for text completions."""

    id: str
    object: Literal["text_completion"] = "text_completion"
    created: int
    model: str
    choices: list[CompletionChoice]
    usage: UsageOut | None = None
