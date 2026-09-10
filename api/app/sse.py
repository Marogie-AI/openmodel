import json

from app.errors import ApiError, envelope

DONE = b"data: [DONE]\n\n"


def sse(data: str) -> bytes:
    return f"data: {data}\n\n".encode()


def error_event(exc: ApiError) -> bytes:
    return sse(json.dumps(envelope(exc)))
