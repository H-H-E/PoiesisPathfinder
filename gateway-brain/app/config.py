from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_path: str = "./data/database.db"
    redis_url: str = "redis://localhost:6379/0"

    portkey_base_url: str = "http://localhost:8787"
    portkey_provider: str = "minimax"
    portkey_config: str | None = None
    portkey_upstream_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PORTKEY_UPSTREAM_API_KEY", "MINIMAX_API_KEY"),
    )

    monthly_token_ceiling: int = 185_700_000
    rate_window_seconds: int = 18_000
    standard_window_limit: int = 642
    high_speed_window_limit: int = 321

    burst_window_seconds: int = 60
    standard_burst_limit: int = 2
    high_speed_burst_limit: int = 1

    dry_run_upstream: bool = False
    allow_streaming: bool = False
    http_timeout_seconds: float = 120.0
    cors_origins: str = "http://localhost:3000"
    poiesis_admin_token: str | None = None
    allow_insecure_admin: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    def long_window_limit_for(self, tier: str) -> int:
        if tier == "high-speed":
            return self.high_speed_window_limit
        return self.standard_window_limit

    def burst_limit_for(self, tier: str) -> int:
        if tier == "high-speed":
            return self.high_speed_burst_limit
        return self.standard_burst_limit


@lru_cache
def get_settings() -> Settings:
    return Settings()
