from typing import Literal

from pydantic import BaseModel


class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int


class ChatResult(BaseModel):
    content: str
    usage: Usage
    finish_reason: Literal["stop", "length"]


class ChatDelta(BaseModel):
    content: str
    done: bool
    usage: Usage | None = None
    finish_reason: Literal["stop", "length"] | None = None


class EmbedResult(BaseModel):
    embeddings: list[list[float]]
    usage: Usage
