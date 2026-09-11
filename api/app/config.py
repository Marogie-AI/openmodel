from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPENMODEL_")

    models_file: Path = Path("models.yaml")
    log_level: str = "INFO"
    environment: str = "dev"
    backend_connect_timeout_s: float = 2.0
    backend_read_timeout_s: float = 120.0
    backend_retries: int = 2
    ready_cache_ttl_s: float = 5.0
    database_url: str = "postgresql+asyncpg://openmodel_app:app@localhost:5432/openmodel"
    database_owner_url: str = "postgresql+asyncpg://openmodel_owner:owner@localhost:5432/openmodel"
    redis_url: str = "redis://localhost:6379/0"
    key_pepper: str = "dev-pepper-change-me"
    admin_token: str = "dev-admin-token"
    auth_cache_ttl_s: int = 300
    db_pool_size: int = 3
    db_max_overflow: int = 2


settings = Settings()
