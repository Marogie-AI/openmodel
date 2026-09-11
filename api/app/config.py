from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPENMODEL_")

    models_file: Path = Path("models.yaml")
    log_level: str = "INFO"
    environment: str = Field("dev", pattern=r"^[a-z]+$")
    backend_connect_timeout_s: float = 2.0
    backend_read_timeout_s: float = 120.0
    backend_retries: int = 2
    ready_cache_ttl_s: float = 5.0
    # How long shutdown waits for in-flight usage writes before giving up on them.
    usage_flush_timeout_s: float = 5.0
    database_url: str = "postgresql+asyncpg://openmodel_app:app@localhost:5432/openmodel"
    database_owner_url: str = "postgresql+asyncpg://openmodel_owner:owner@localhost:5432/openmodel"
    redis_url: str = "redis://localhost:6379/0"
    key_pepper: str = Field("dev-pepper-change-me", min_length=16)
    admin_token: str = Field("dev-admin-token-change-me", min_length=16)
    auth_cache_ttl_s: int = 300
    db_pool_size: int = 3
    db_max_overflow: int = 2


settings = Settings()
