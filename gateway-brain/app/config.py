from functools import lru_cache
from typing import Literal

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
    default_request_tier: Literal["standard", "high-speed"] = "standard"
    high_speed_student_ids: str = ""

    burst_window_seconds: int = 60
    standard_burst_limit: int = 2
    high_speed_burst_limit: int = 1

    dry_run_upstream: bool = False
    allow_streaming: bool = False
    max_request_body_bytes: int = 262_144
    max_messages: int = 64
    max_message_content_chars: int = 16_000
    max_total_message_content_chars: int = 64_000
    http_timeout_seconds: float = 120.0
    cors_origins: str = "http://localhost:3000"
    poiesis_admin_token: str | None = None
    allow_insecure_admin: bool = False
    poiesis_key_hash_secret: str = "replace-with-local-key-hash-secret"

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

    @property
    def high_speed_student_id_set(self) -> set[str]:
        return {
            student_id.strip()
            for student_id in self.high_speed_student_ids.split(",")
            if student_id.strip()
        }

    def request_tier_for_student(self, student_id: str) -> Literal["standard", "high-speed"]:
        if student_id in self.high_speed_student_id_set:
            return "high-speed"
        return self.default_request_tier


@lru_cache
def get_settings() -> Settings:
    return Settings()
