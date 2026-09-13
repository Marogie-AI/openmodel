from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.backend import Usage


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None


class UsageOut(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int = 0

    @model_validator(mode="after")
    def _total(self) -> "UsageOut":
        self.total_tokens = self.prompt_tokens + self.completion_tokens
        return self

    @classmethod
    def of(cls, usage: Usage) -> "UsageOut":
        return cls(prompt_tokens=usage.prompt_tokens, completion_tokens=usage.completion_tokens)


class ChatChoice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: str


class ChatCompletion(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[ChatChoice]
    usage: UsageOut


class ChunkChoice(BaseModel):
    index: int = 0
    delta: dict[str, str]
    finish_reason: str | None = None


class ChatCompletionChunk(BaseModel):
    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int
    model: str
    choices: list[ChunkChoice]
    usage: UsageOut | None = None
