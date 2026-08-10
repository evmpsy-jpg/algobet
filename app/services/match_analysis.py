from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
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


def _html(value: Any) -> str:
    return escape(str(value or ""), quote=False)


def _match_link(player_1_line: str, player_2_line: str, source_url: str | None) -> str:
    label = f"{player_1_line} ⚔️ {player_2_line}"
    href = str(source_url or "").strip()
    if not href:
        return _html(label)
    return f"<a href=\"{escape(href, quote=True)}\">{_html(label)}</a>"


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

    def get_first(*fields: str) -> float | None:
        for field in fields:
            value = get(field)
            if value is not None:
                return value
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
        advantage=get("D"),
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
        h2h_p1=get_first("AA", "P1"),
        h2h_p2=get_first("AB", "P2"),
        average_h2h_handicap=get("AI"),
        average_difference=get("EF"),
        set1_handicap=get("DS"),
        set2_handicap=get("DV"),
        set3_handicap=get("DY"),
        set4_handicap=get("EB"),
        set5_handicap=get("EE"),
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


def _flipped_value(value: Any, side: int) -> Any:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    return number if side == 1 else -number

def _number_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None



def _match_advantage_text(value: Any) -> str:
    number = _number_or_none(value)
    if number is None:
        return "⚪ Нет преимущества"
    if number > 0:
        return f"🟢 Преимущество П1 = {_fmt_number(abs(number), signed=False, digits=0)} Бал."
    if number < 0:
        return f"🔴 Преимущество П2 = {_fmt_number(abs(number), signed=False, digits=0)} Бал."
    return "⚪ Нет преимущества"

def _set_advantage_line(index: int, value: Any) -> str:
    number = _number_or_none(value)
    if number is None:
        return f"{index}️⃣ Сет:"
    formatted = _fmt_number(value, signed=True)
    if number is None or abs(number) <= 2.2:
        suffix = "нет явного преимущества"
    else:
        side = "П1" if number > 0 else "П2"
        suffix = f"явное преимущество {side}"
    return f"{index}️⃣ Сет: {formatted} - {suffix}"


def _has_no_clear_favorite(value: Any) -> bool:
    number = _number_or_none(value)
    return number is not None and abs(number) < 2



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
        "🚀 <b>Как заказать аналитику матча:</b>",
        "1️⃣ <b>Выберите</b> предстоящий матч в меню.",
        "2️⃣ <b>Оплатите</b> услугу (стоимость: 100 ₽).",
        "3️⃣ <b>Нажмите</b> кнопку <b>«Я оплатил»</b> под этим сообщением.",
        "⏱️ <i>Аналитика придет автоматически сразу после подтверждения платежа!</i>",
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
    favorite_player = data.player_1 if side == 1 else data.player_2
    set1 = data.set1_handicap
    set2 = data.set2_handicap
    set3 = data.set3_handicap
    set4 = data.set4_handicap
    set5 = data.set5_handicap
    average_h2h_handicap = _flipped_value(data.average_h2h_handicap, side)
    average_difference = _flipped_value(data.average_difference, side)

    player_1 = f"({data.player_1_rating}) {data.player_1}" if data.player_1_rating else data.player_1
    player_2 = f"({data.player_2_rating}) {data.player_2}" if data.player_2_rating else data.player_2

    lines = [
        "🎯 Анализ матча",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"🏓 {_tournament_line(data.tournament_name)}",
        f"🕐 Начало: {_fmt_dt(data.match_start_at, '%H:%M')} МСК",
        "",
        f"📌 {_match_link(player_1, player_2, data.source_url)}",
        "",
        f"🎯П1 = {_fmt_number(data.all_signal_p1, signed=False, digits=0)} Бал. ⚔️ П2 = {_fmt_number(data.all_signal_p2, signed=False, digits=0)} Бал. // из 10",
        "",
        _match_advantage_text(data.advantage),
        "",
        f" Вероятность: {_fmt_number(data.probability_p1, signed=False, digits=0)}% ⚔️   {_fmt_number(data.probability_p2, signed=False, digits=0)}%",
        f" Форма фаворита: {_fmt_number(data.favorite_form_p1, signed=False, digits=0)}% ⚔️   {_fmt_number(data.favorite_form_p2, signed=False, digits=0)}%",
        "",
        f"👉 Фаворит по игре:  {favorite_player}",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        "🏓 Фора по мячам за последние 5 H2H",
        "",
        _set_advantage_line(1, set1),
        _set_advantage_line(2, set2),
        _set_advantage_line(3, set3),
        _set_advantage_line(4, set4),
        _set_advantage_line(5, set5),
        "",
        "🔥 Средняя разница относительно фаворита:",
        f" {_fmt_number(average_difference, signed=True)} очков.",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        "📊 Общая статистика H2H",
        "",
        f"Победы: {_fmt_number(data.h2h_p1, signed=False, digits=0)} ⚔️  {_fmt_number(data.h2h_p2, signed=False, digits=0)}",
        "",
        "📈 Средняя фора относительно фаворита:",
        f"{_fmt_number(average_h2h_handicap, signed=True)} ({_fmt_number(data.h2h_games, signed=False, digits=0)} игр в H2H)",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
    ]
    if _has_no_clear_favorite(average_difference):
        lines.extend(["", "Нет явного фаворита."])
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
