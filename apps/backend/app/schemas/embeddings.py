from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class EmbeddingRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str
    input: str | Annotated[list[str], Field(min_length=1)]


class Embedding(BaseModel):
    object: Literal["embedding"] = "embedding"
    index: int
    embedding: list[float]


class EmbeddingUsage(BaseModel):
    prompt_tokens: int
    total_tokens: int


class EmbeddingList(BaseModel):
    object: Literal["list"] = "list"
    data: list[Embedding]
    model: str
    usage: EmbeddingUsage
