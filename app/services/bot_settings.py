from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import BotSetting
from app.settings import get_settings

ANALYSIS_PAYMENT_DETAILS_KEY = "analysis.payment_details"
ANALYSIS_SPECIALIST_CONTACT_KEY = "analysis.specialist_contact"


@dataclass(frozen=True)
class AnalysisPaymentConfig:
    payment_details: str
    specialist_contact: str


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


async def get_analysis_payment_config(session: AsyncSession) -> AnalysisPaymentConfig:
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
    return AnalysisPaymentConfig(payment_details=payment_details, specialist_contact=specialist_contact)
