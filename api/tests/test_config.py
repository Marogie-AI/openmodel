import pytest
from pydantic import ValidationError

from app.auth.keys import generate_key
from app.config import Settings


@pytest.mark.parametrize(
    "kwargs",
    [
        {"admin_token": ""},
        {"admin_token": "short"},
        {"key_pepper": ""},
        {"key_pepper": "short"},
        {"environment": "prod-eu"},
        {"environment": ""},
        {"environment": "Prod"},
    ],
)
def test_settings_rejects_unsafe_values(kwargs: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        Settings(**kwargs)


def test_settings_accepts_sane_values() -> None:
    settings = Settings(environment="prod", admin_token="x" * 16, key_pepper="y" * 16)

    assert settings.environment == "prod"


def test_generate_key_rejects_an_env_that_would_break_the_key_format() -> None:
    with pytest.raises(ValueError, match=r"\^\[a-z\]\+\$"):
        generate_key("prod-eu")
