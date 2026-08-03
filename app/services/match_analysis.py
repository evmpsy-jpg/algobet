from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Match, MatchAnalysisRequest, User
from app.domain.models import MatchData
from app.settings import get_settings

MIN_MATCH_TEXT_LENGTH = 5
MAX_MATCH_TEXT_LENGTH = 2000


@dataclass(frozen=True)
class AnalysisRequestTexts:
    user_text: str
    admin_text: str


def validate_match_analysis_text(value: str) -> str:
    text = value.strip()
    if len(text) < MIN_MATCH_TEXT_LENGTH:
        raise ValueError("Минимальная длина текста: не менее 5 символов.")
    if len(text) > MAX_MATCH_TEXT_LENGTH:
        raise ValueError("Слишком длинный текст. Ограничьтесь 2000 символами.")
    return text


def _local_dt(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(ZoneInfo(get_settings().timezone))


def _fmt_dt(value: datetime | None, fmt: str = "%d.%m %H:%M") -> str:
    local = _local_dt(value)
    return local.strftime(fmt) if local else "—"


def _fmt_number(value: Any, *, signed: bool = False, digits: int = 1) -> str:
    if value is None or value == "":
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if number == 0:
        return "0"
    if number.is_integer():
        return f"{number:+.0f}" if signed else f"{number:.0f}"
    return f"{number:+.{digits}f}" if signed else f"{number:.{digits}f}"


def _tournament_line(name: str) -> str:
    cleaned = (name or "Турнир").strip()
    parts = cleaned.split(".", 1)
    if len(parts) == 2 and parts[1].strip():
        return f"{parts[0].strip()} • {parts[1].strip()}"
    return cleaned


def _match_data(match: Match) -> MatchData:
    raw_data = match.raw_data if isinstance(match.raw_data, dict) else {}

    def get(field: str) -> float | None:
        value = raw_data.get(field)
        if value is None or value == "":
            return None
        try:
            return float(str(value).replace("%", "").replace(",", ".").strip())
        except (TypeError, ValueError):
            return None

    return MatchData(
        match_id=match.external_match_id,
        tournament_id=match.external_tournament_id,
        source_url=match.source_url,
        tournament_date=match.tournament_date,
        tournament_name=str(raw_data.get("_tournament_name") or ""),
        match_time=match.match_time,
        match_start_at=match.match_start_at,
        player_1=match.player_1,
        player_2=match.player_2,
        player_1_rating=match.player_1_rating,
        player_2_rating=match.player_2_rating,
        score=match.score,
        h2h_games=get("CP"),
        form_p1=get("Q"),
        form_p2=get("X"),
        favorite_form_p1=get("EJ"),
        favorite_form_p2=get("EK"),
        bg_p1=get("BG"),
        bf_p2=get("BF"),
        probability_p1=get("CV"),
        probability_p2=get("CW"),
        all_signal_p1=get("DG"),
        all_signal_p2=get("DH"),
        p1_exact=get("EG"),
        p2_exact=get("EH"),
        p1_range=get("CS"),
        p2_range=get("CT"),
        h2h_p1=get("P1"),
        h2h_p2=get("P2"),
        average_h2h_handicap=get("AI"),
        average_difference=get("EF"),
        set1_handicap=get("AM"),
        set2_handicap=get("AN"),
        set3_handicap=get("AO"),
        raw_data=raw_data,
    )


def _pick_favorite_side(match: MatchData) -> int:
    p1_prob = match.probability_p1
    p2_prob = match.probability_p2
    if p1_prob is not None and p2_prob is not None and p1_prob != p2_prob:
        return 1 if p1_prob > p2_prob else 2
    if p1_prob is not None:
        return 1
    if p2_prob is not None:
        return 2

    p1_form = match.favorite_form_p1
    p2_form = match.favorite_form_p2
    if p1_form is not None and p2_form is not None and p1_form != p2_form:
        return 1 if p1_form > p2_form else 2
    if p1_form is not None:
        return 1
    if p2_form is not None:
        return 2

    return 1


def _selected_value(match: MatchData, side: int, p1_value: Any, p2_value: Any) -> Any:
    return p1_value if side == 1 else p2_value


def _flipped_value(value: Any, side: int) -> Any:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    return number if side == 1 else -number


def _match_title(match: Match) -> str:
    return f"{_fmt_dt(match.match_start_at, '%d.%m %H:%M')} · {match.player_1} — {match.player_2}"


async def get_upcoming_matches(session: AsyncSession, *, limit: int = 10, after: datetime | None = None) -> list[Match]:
    after = after or datetime.utcnow()
    rows = await session.scalars(
        select(Match)
        .where(Match.match_start_at > after)
        .order_by(Match.match_start_at.asc(), Match.id.asc())
        .limit(limit)
    )
    return list(rows.all())


def format_upcoming_matches_text(matches: list[Match]) -> str:
    lines = [
        "🔎 Анализ матча",
        "",
        "Выберите один из ближайших матчей:",
    ]
    if not matches:
        lines.append("Пока нет ближайших матчей для анализа.")
        return "\n".join(lines)
    for index, match in enumerate(matches, start=1):
        lines.append(f"{index}. {_match_title(match)}")
    return "\n".join(lines)


def format_analysis_request_user_text(request: MatchAnalysisRequest) -> str:
    lines = [
        "🎯 Анализ матча",
        "",
        f"Заявка: #{request.id}",
        f"Матч: {request.match_title or request.match_text}",
        "",
        "Реквизиты для оплаты:",
        request.payment_details or "Реквизиты пока не настроены.",
        "",
        "После оплаты нажмите кнопку ниже.",
    ]
    return "\n".join(lines)[:3900]


def format_analysis_payment_text(request: MatchAnalysisRequest) -> str:
    lines = [
        f"✅ Заявка на анализ #{request.id}",
        "",
        "Спасибо. Платёж отмечен.",
        "",
        "Реквизиты для проверки:",
        request.payment_details or "Реквизиты пока не настроены.",
        "",
        "Ожидайте выдачи анализа.",
    ]
    return "\n".join(lines)[:3900]


def format_analysis_request_admin_text(request: MatchAnalysisRequest, user: User) -> str:
    username = f"@{user.username}" if user.username else "—"
    name_parts = [item for item in [user.first_name, user.last_name] if item]
    name = " ".join(name_parts) if name_parts else "—"
    lines = [
        "🆕 Новая заявка на анализ матча",
        "",
        f"Заявка: #{request.id}",
        f"Пользователь: {name}",
        f"Имя пользователя: {username}",
        f"ID Telegram: {user.telegram_id}",
        "",
        "Матч:",
        request.match_title or request.match_text,
    ]
    return "\n".join(lines)[:3900]


def format_analysis_payment_admin_text(request: MatchAnalysisRequest, user: User) -> str:
    username = f"@{user.username}" if user.username else "—"
    name_parts = [item for item in [user.first_name, user.last_name] if item]
    name = " ".join(name_parts) if name_parts else "—"
    lines = [
        "💳 Клиент оплатил заявку на анализ матча",
        "",
        f"Заявка: #{request.id}",
        f"Пользователь: {name}",
        f"Имя пользователя: {username}",
        f"ID Telegram: {user.telegram_id}",
        "",
        "Матч:",
        request.match_title or request.match_text,
        "",
        "Нажмите кнопку ниже, чтобы отправить анализ клиенту.",
    ]
    return "\n".join(lines)[:3900]


async def create_match_analysis_request(
    session: AsyncSession,
    user: User,
    match: Match,
    *,
    payment_details: str,
    specialist_contact: str,
) -> MatchAnalysisRequest:
    request = MatchAnalysisRequest(
        user_id=user.id,
        telegram_id=user.telegram_id,
        username=user.username,
        match_id=match.id,
        match_title=_match_title(match),
        match_text=_match_title(match),
        match_start_at=match.match_start_at,
        payment_details=payment_details,
        specialist_contact=specialist_contact,
    )
    session.add(request)
    await session.flush()
    return request


def build_match_analysis_text(match: Match) -> str:
    data = _match_data(match)
    side = _pick_favorite_side(data)
    probability = _selected_value(data, side, data.probability_p1, data.probability_p2)
    favorite_form = _selected_value(data, side, data.favorite_form_p1, data.favorite_form_p2)
    favorite_player = data.player_1 if side == 1 else data.player_2
    h2h_wins_favorite = _selected_value(data, side, data.h2h_p1, data.h2h_p2)
    h2h_wins_opponent = _selected_value(data, side, data.h2h_p2, data.h2h_p1)
    total_h2h = data.h2h_games
    recent_h2h = _selected_value(data, side, data.p1_exact, data.p2_exact)
    set1 = _flipped_value(data.set1_handicap, side)
    set2 = _flipped_value(data.set2_handicap, side)
    set3 = _flipped_value(data.set3_handicap, side)
    average_h2h_handicap = _flipped_value(data.average_h2h_handicap, side)
    average_difference = _flipped_value(data.average_difference, side)
    color = "🟢" if side == 1 else "🔴"

    lines = [
        "🎯 Анализ матча",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"🏓 {_tournament_line(data.tournament_name)}",
        f"🕐 Начало: {_fmt_dt(data.match_start_at, '%H:%M')} МСК",
        "",
        f"📌 {data.player_1}",
        "⚔️",
        f"{data.player_2}",
        "",
        f"{color} Вероятность: {_fmt_number(probability, signed=False, digits=0)}%",
        f"{color} Форма фаворита: {_fmt_number(favorite_form, signed=False, digits=0)}%",
        "",
        f"👉 Фаворит: {favorite_player}",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        "🏓 Фора по мячам за последние 5 H2H",
        "относительно фаворита:",
        "",
        f"1️⃣ Сет: {_fmt_number(set1, signed=True)}",
        f"2️⃣ Сет: {_fmt_number(set2, signed=True)}",
        f"3️⃣ Сет: {_fmt_number(set3, signed=True)}",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        "📊 Общая статистика H2H",
        "",
        f"Победы: {_fmt_number(h2h_wins_favorite, signed=False, digits=0)} / {_fmt_number(h2h_wins_opponent, signed=False, digits=0)}",
        "",
        "📈 Средняя фора относительно фаворита:",
        f"{_fmt_number(average_h2h_handicap, signed=True)} ({_fmt_number(total_h2h, signed=False, digits=0)} игр в H2H)",
        "",
        "🔥 Средняя разница относительно фаворита:",
        f"{_fmt_number(average_difference, signed=True)} очков ({_fmt_number(recent_h2h, signed=False, digits=0)}/5 в H2H)",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
    ]
    return "\n".join(lines)[:3900]


def format_analysis_status_user_text(request: MatchAnalysisRequest) -> str:
    status_messages = {
        "paid": "Спасибо. Заявка принята в работу.",
        "in_progress": "Ваш анализ уже готовится специалистом.",
        "done": "Анализ готов. Ожидайте сообщение от специалиста.",
        "cancelled": "Заявка отменена. Если это ошибка, свяжитесь со специалистом.",
    }
    message = status_messages.get(request.status)
    if message is None:
        return ""
    return "\n".join(
        [
            f"✅ Заявка на анализ #{request.id}",
            "",
            message,
            "",
            f"Контакт специалиста: {request.specialist_contact or '—'}",
        ]
    )
