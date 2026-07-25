from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import BotSetting
from app.settings import get_settings

ANALYSIS_PAYMENT_DETAILS_KEY = "analysis.payment_details"
ANALYSIS_SPECIALIST_CONTACT_KEY = "analysis.specialist_contact"
SUBSCRIPTION_PAYMENT_DETAILS_KEY = "subscription.payment_details"
SUBSCRIPTION_SPECIALIST_CONTACT_KEY = "subscription.specialist_contact"
SYSTEM_SQLITE_BACKUP_ENABLED_KEY = "system.sqlite_backup_enabled"
SYSTEM_SQLITE_BACKUP_INTERVAL_HOURS_KEY = "system.sqlite_backup_interval_hours"
SYSTEM_SQLITE_BACKUP_KEEP_KEY = "system.sqlite_backup_keep"


@dataclass(frozen=True)
class PaymentConfig:
    payment_details: str
    specialist_contact: str


@dataclass(frozen=True)
class SystemRuntimeSettings:
    sqlite_backup_enabled: bool
    sqlite_backup_interval_hours: int
    sqlite_backup_keep: int


def normalize_bot_setting_value(value: str, *, max_length: int = 2000) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("Значение не может быть пустым.")
    if len(cleaned) > max_length:
        raise ValueError(f"Значение слишком длинное. Максимум: {max_length} символов.")
    return cleaned


async def get_bot_setting(session: AsyncSession, key: str, default: str) -> str:
    setting = await session.scalar(select(BotSetting).where(BotSetting.key == key))
    if setting is None or not setting.value.strip():
        return default
    return setting.value


async def set_bot_setting(session: AsyncSession, key: str, value: str, *, max_length: int = 2000) -> BotSetting:
    cleaned = normalize_bot_setting_value(value, max_length=max_length)
    setting = await session.scalar(select(BotSetting).where(BotSetting.key == key))
    if setting is None:
        setting = BotSetting(key=key, value=cleaned)
        session.add(setting)
    else:
        setting.value = cleaned
        setting.updated_at = datetime.utcnow()
    await session.flush()
    return setting


async def get_analysis_payment_config(session: AsyncSession) -> PaymentConfig:
    settings = get_settings()
    payment_details = await get_bot_setting(
        session,
        ANALYSIS_PAYMENT_DETAILS_KEY,
        settings.analysis_payment_details,
    )
    specialist_contact = await get_bot_setting(
        session,
        ANALYSIS_SPECIALIST_CONTACT_KEY,
        settings.analysis_specialist_contact,
    )
    return PaymentConfig(payment_details=payment_details, specialist_contact=specialist_contact)

async def get_subscription_payment_config(session: AsyncSession) -> PaymentConfig:
    settings = get_settings()
    payment_details = await get_bot_setting(
        session,
        SUBSCRIPTION_PAYMENT_DETAILS_KEY,
        settings.analysis_payment_details,
    )
    specialist_contact = await get_bot_setting(
        session,
        SUBSCRIPTION_SPECIALIST_CONTACT_KEY,
        settings.analysis_specialist_contact,
    )
    return PaymentConfig(payment_details=payment_details, specialist_contact=specialist_contact)


def _parse_bool_setting(value: str, default: bool) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _parse_int_setting(value: str, default: int, *, min_value: int, max_value: int) -> int:
    try:
        parsed = int(value.strip())
    except (TypeError, ValueError):
        return default
    return min(max(parsed, min_value), max_value)


async def get_system_runtime_settings(session: AsyncSession, settings=None) -> SystemRuntimeSettings:
    settings = settings or get_settings()
    enabled_text = await get_bot_setting(session, SYSTEM_SQLITE_BACKUP_ENABLED_KEY, "1" if settings.sqlite_backup_enabled else "0")
    interval_text = await get_bot_setting(session, SYSTEM_SQLITE_BACKUP_INTERVAL_HOURS_KEY, str(settings.sqlite_backup_interval_hours))
    keep_text = await get_bot_setting(session, SYSTEM_SQLITE_BACKUP_KEEP_KEY, str(settings.sqlite_backup_keep))
    return SystemRuntimeSettings(
        sqlite_backup_enabled=_parse_bool_setting(enabled_text, bool(settings.sqlite_backup_enabled)),
        sqlite_backup_interval_hours=_parse_int_setting(interval_text, int(settings.sqlite_backup_interval_hours), min_value=1, max_value=168),
        sqlite_backup_keep=_parse_int_setting(keep_text, int(settings.sqlite_backup_keep), min_value=1, max_value=60),
    )


async def set_system_runtime_settings(
    session: AsyncSession,
    *,
    sqlite_backup_enabled: bool,
    sqlite_backup_interval_hours: int,
    sqlite_backup_keep: int,
) -> SystemRuntimeSettings:
    interval = min(max(int(sqlite_backup_interval_hours), 1), 168)
    keep = min(max(int(sqlite_backup_keep), 1), 60)
    await set_bot_setting(session, SYSTEM_SQLITE_BACKUP_ENABLED_KEY, "1" if sqlite_backup_enabled else "0", max_length=10)
    await set_bot_setting(session, SYSTEM_SQLITE_BACKUP_INTERVAL_HOURS_KEY, str(interval), max_length=10)
    await set_bot_setting(session, SYSTEM_SQLITE_BACKUP_KEEP_KEY, str(keep), max_length=10)
    return SystemRuntimeSettings(
        sqlite_backup_enabled=sqlite_backup_enabled,
        sqlite_backup_interval_hours=interval,
        sqlite_backup_keep=keep,
    )


def apply_system_runtime_settings(settings, runtime: SystemRuntimeSettings):
    class EffectiveSettings:
        def __init__(self, base, overrides: SystemRuntimeSettings):
            self._base = base
            self.sqlite_backup_enabled = overrides.sqlite_backup_enabled
            self.sqlite_backup_interval_hours = overrides.sqlite_backup_interval_hours
            self.sqlite_backup_keep = overrides.sqlite_backup_keep

        def __getattr__(self, name: str):
            return getattr(self._base, name)

    return EffectiveSettings(settings, runtime)
