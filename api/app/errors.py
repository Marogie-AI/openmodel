class ApiError(Exception):
    """Base for errors rendered as the OpenAI error envelope."""

    status_code: int = 500
    type: str = "server_error"
    code: str | None = None

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def envelope(exc: ApiError) -> dict[str, object]:
    """The OpenAI error envelope, shared by the exception handler and in-stream error events."""
    return {"error": {"message": exc.message, "type": exc.type, "code": exc.code}}


class UnknownModel(ApiError):
    status_code = 404
    type = "invalid_request_error"
    code = "model_not_found"

    def __init__(self, name: str) -> None:
        super().__init__(f"The model '{name}' does not exist.")


class CapabilityMismatch(ApiError):
    status_code = 400
    type = "invalid_request_error"
    code = "model_capability"

    def __init__(self, name: str, capability: str) -> None:
        super().__init__(f"The model '{name}' does not support {capability}.")


class PromptListUnsupported(ApiError):
    status_code = 400
    type = "invalid_request_error"
    code = "prompt_list_unsupported"

    def __init__(self) -> None:
        super().__init__("Batched prompts are not supported; send a single prompt.")


class InvalidRequest(ApiError):
    status_code = 422
    type = "invalid_request_error"


class BackendUnavailable(ApiError):
    status_code = 502
    type = "server_error"
    code = "backend_unavailable"


class BackendTimeout(ApiError):
    status_code = 504
    type = "server_error"
    code = "backend_timeout"


class Unauthorized(ApiError):
    status_code = 401
    type = "invalid_request_error"
    code = "invalid_api_key"


class Forbidden(ApiError):
    status_code = 403
    type = "invalid_request_error"
    code = "forbidden"


class NotFound(ApiError):
    status_code = 404
    type = "invalid_request_error"
    code = "not_found"


class Conflict(ApiError):
    status_code = 409
    type = "invalid_request_error"
    code = "conflict"


class RateLimited(ApiError):
    status_code = 429
    type = "rate_limit_error"
    code = "rate_limit_exceeded"

    def __init__(self, message: str, *, retry_after_s: int, limit: int, remaining: int) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s
        self.limit = limit
        self.remaining = remaining


class ModelNotAllowed(ApiError):
    status_code = 403
    type = "invalid_request_error"
    code = "model_not_allowed"

    def __init__(self, name: str) -> None:
        super().__init__(f"Your plan does not include the model '{name}'.")


class ServiceUnavailable(ApiError):
    """A dependency this request needs is down. Fail closed: never serve unlimited or unmetered."""

    status_code = 503
    type = "server_error"


class RateLimiterUnavailable(ServiceUnavailable):
    code = "rate_limiter_unavailable"

    def __init__(self) -> None:
        super().__init__("Rate limiting is unavailable; the request was not served.")


class DatabaseUnavailable(ServiceUnavailable):
    code = "database_unavailable"

    def __init__(self) -> None:
        super().__init__("The database is unavailable; the request was not served.")
