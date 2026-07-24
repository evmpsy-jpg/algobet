from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import MatchAnalysisRequest, User

MIN_MATCH_TEXT_LENGTH = 5
MAX_MATCH_TEXT_LENGTH = 2000


@dataclass(frozen=True)
class AnalysisRequestTexts:
    user_text: str
    admin_text: str


def validate_match_analysis_text(value: str) -> str:
    text = value.strip()
    if len(text) < MIN_MATCH_TEXT_LENGTH:
        raise ValueError("Опишите матч чуть подробнее: игроки, турнир, время или ссылка.")
    if len(text) > MAX_MATCH_TEXT_LENGTH:
        raise ValueError("Описание слишком длинное. Отправьте до 2000 символов.")
    return text


def format_analysis_request_user_text(request: MatchAnalysisRequest) -> str:
    lines = [
        "🔎 Заявка на анализ матча принята",
        "",
        f"Номер заявки: #{request.id}",
        "",
        "Реквизиты для оплаты:",
        request.payment_details or "Реквизиты уточните у специалиста.",
        "",
        "Контакт специалиста:",
        request.specialist_contact or "Контакт специалиста уточняется.",
        "",
        "После оплаты напишите специалисту номер заявки.",
    ]
    return "\n".join(lines)[:3900]


def format_analysis_request_admin_text(request: MatchAnalysisRequest, user: User) -> str:
    username = f"@{user.username}" if user.username else "—"
    name_parts = [item for item in [user.first_name, user.last_name] if item]
    name = " ".join(name_parts) if name_parts else "—"
    lines = [
        "🔎 Новая заявка на анализ матча",
        "",
        f"Заявка: #{request.id}",
        f"Пользователь: {name}",
        f"Имя пользователя: {username}",
        f"ID Telegram: {user.telegram_id}",
        "",
        "Матч:",
        request.match_text,
    ]
    return "\n".join(lines)[:3900]


async def create_match_analysis_request(
    session: AsyncSession,
    user: User,
    match_text: str,
    *,
    payment_details: str,
    specialist_contact: str,
) -> MatchAnalysisRequest:
    request = MatchAnalysisRequest(
        user_id=user.id,
        telegram_id=user.telegram_id,
        username=user.username,
        match_text=validate_match_analysis_text(match_text),
        payment_details=payment_details,
        specialist_contact=specialist_contact,
    )
    session.add(request)
    await session.flush()
    return request


def format_analysis_status_user_text(request: MatchAnalysisRequest) -> str:
    status_messages = {
        "paid": "Оплата по заявке отмечена.",
        "in_progress": "Специалист взял заявку в работу.",
        "done": "Анализ готов. Специалист свяжется с вами по заявке.",
        "cancelled": "Заявка отменена. Если это ошибка, напишите специалисту.",
    }
    message = status_messages.get(request.status)
    if message is None:
        return ""
    return "\n".join([
        f"🔎 Заявка на анализ #{request.id}",
        "",
        message,
        "",
        f"Контакт специалиста: {request.specialist_contact or '—'}",
    ])
