class ApiError(Exception):
    """Base for errors rendered as the OpenAI error envelope."""

    status_code: int = 500
    type: str = "server_error"
    code: str | None = None

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


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


class BackendUnavailable(ApiError):
    status_code = 502
    type = "server_error"
    code = "backend_unavailable"


class BackendTimeout(ApiError):
    status_code = 504
    type = "server_error"
    code = "backend_timeout"
