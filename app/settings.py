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
    sqlite_backup_enabled: bool = Field(default=True, alias="SQLITE_BACKUP_ENABLED")
    sqlite_backup_interval_hours: int = Field(default=24, alias="SQLITE_BACKUP_INTERVAL_HOURS")
    sqlite_backup_keep: int = Field(default=10, alias="SQLITE_BACKUP_KEEP")
    web_admin_users_text: str = Field(default="", alias="WEB_ADMIN_USERS")
    web_admin_username: str = Field(default="", alias="WEB_ADMIN_USERNAME")
    web_admin_password: str = Field(default="", alias="WEB_ADMIN_PASSWORD")
    web_admin_superusers_text: str = Field(default="", alias="WEB_ADMIN_SUPERUSERS")
    analysis_payment_details: str = Field(default="Реквизиты для оплаты уточните у специалиста.", alias="ANALYSIS_PAYMENT_DETAILS")
    analysis_specialist_contact: str = Field(default="@your_specialist", alias="ANALYSIS_SPECIALIST_CONTACT")
    google_sheets_sync_enabled: bool = Field(default=False, alias="GOOGLE_SHEETS_SYNC_ENABLED")
    google_sheet_id: str = Field(default="", alias="GOOGLE_SHEET_ID")
    google_service_account_file: str = Field(default="", alias="GOOGLE_SERVICE_ACCOUNT_FILE")
    google_sheets_sync_interval_minutes: int = Field(default=20, alias="GOOGLE_SHEETS_SYNC_INTERVAL_MINUTES")
    google_sheets_sync_schedule_minutes: str = Field(default="", alias="GOOGLE_SHEETS_SYNC_SCHEDULE_MINUTES")
    google_sheets_sync_max_rows: int = Field(default=1200, alias="GOOGLE_SHEETS_SYNC_MAX_ROWS")

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    @property
    def web_admin_credentials(self) -> dict[str, str]:
        credentials: dict[str, str] = {}
        for item in self.web_admin_users_text.split(","):
            username, separator, password = item.strip().partition(":")
            if username and separator and password:
                credentials[username] = password
        fallback_username = self.web_admin_username.strip()
        fallback_password = self.web_admin_password.strip()
        if fallback_username and fallback_password:
            credentials.setdefault(fallback_username, fallback_password)
        return credentials

    @property
    def web_admin_superusers(self) -> set[str]:
        configured = {item.strip() for item in self.web_admin_superusers_text.split(",") if item.strip()}
        return configured or set(self.web_admin_credentials)

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
