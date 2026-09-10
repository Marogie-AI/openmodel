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


settings = Settings()
