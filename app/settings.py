from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bot_token: str = Field(alias="BOT_TOKEN")
    admin_ids_text: str = Field(default="", alias="ADMIN_IDS")
    database_url: str = Field(
        default="sqlite+aiosqlite:///./data/algobet.db",
        alias="DATABASE_URL",
    )
    timezone: str = Field(default="Europe/Moscow", alias="TIMEZONE")
    signal_lead_minutes: int = Field(default=20, alias="SIGNAL_LEAD_MINUTES")
    scheduler_interval_seconds: int = Field(default=30, alias="SCHEDULER_INTERVAL_SECONDS")
    max_upload_mb: int = Field(default=25, alias="MAX_UPLOAD_MB")

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    @property
    def admin_ids(self) -> list[int]:
        return [int(item.strip()) for item in self.admin_ids_text.split(",") if item.strip()]

    @property
    def uploads_dir(self) -> Path:
        path = Path("uploads")
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def data_dir(self) -> Path:
        path = Path("data")
        path.mkdir(parents=True, exist_ok=True)
        return path


@lru_cache
def get_settings() -> Settings:
    return Settings()
