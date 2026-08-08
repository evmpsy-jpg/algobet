from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import asc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Match, PromoSignalPost, ScheduledSignal, SignalResult
from app.settings import get_settings


class PromoBot(Protocol):
    async def send_message(self, **kwargs: Any) -> Any:
        ...


@dataclass(slots=True)
class PromoPublishResult:
    status: str
    message: str
    signal_id: int | None = None
    telegram_message_id: int | None = None


def _local_dt(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    tz = ZoneInfo(get_settings().timezone)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(tz)


def _fmt_start(value: datetime | None) -> str:
    local = _local_dt(value)
    return local.strftime("%H:%M") if local else "--:--"


def _tournament_line(match: Match) -> str:
    raw_data = match.raw_data or {}
    name = str(raw_data.get("_tournament_name") or raw_data.get("tournament_name") or "Турнир").strip()
    if ". " in name:
        head, tail = name.split(". ", 1)
        return f"{head} • {tail}"
    return name


def _player_with_rating(name: str, rating: int | None) -> str:
    return f"({rating}) {name}" if rating else name


def _signal_level(payload: dict[str, Any] | None) -> str:
    group = str((payload or {}).get("signal_group") or "").strip().lower()
    return "VIP" if group == "vip" else "STANDART"


def _selected_player(signal: ScheduledSignal, match: Match) -> str:
    payload = signal.signal_payload or {}
    selected = str(payload.get("selected_player") or "").strip()
    if selected:
        return selected
    side = payload.get("side")
    if side == 1:
        return match.player_1
    if side == 2:
        return match.player_2
    return "—"


def _result_line(status: str | None) -> str:
    if status == "won":
        return "🟢 ЗАШЕЛ"
    if status == "lost":
        return "🔴 НЕ ЗАШЕЛ"
    return "⚪ РЕЗУЛЬТАТ НЕИЗВЕСТЕН"


def format_promo_signal_post(signal: ScheduledSignal, match: Match, result: SignalResult) -> str:
    payload = signal.signal_payload or {}
    lines = [
        "🎯 <b>СИГНАЛ — НА СЕТ</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Уровень: {_signal_level(payload)}",
        "",
        f"⏳ Старт: {_fmt_start(match.match_start_at)} МСК",
        f"🏓 Турнир: {escape(_tournament_line(match), quote=False)}",
        (
            "📌 Матч: "
            f"{escape(_player_with_rating(match.player_1, match.player_1_rating), quote=False)}"
            " ⚔️ "
            f"{escape(_player_with_rating(match.player_2, match.player_2_rating), quote=False)}"
        ),
        "",
        f"👉 Выбор: Победа в сете - {escape(_selected_player(signal, match), quote=False)}",
        "",
        f"Счет матча: {escape(str(match.score or '—'), quote=False)}",
        "",
        _result_line(result.status),
    ]
    return "\n".join(lines)


async def publish_next_played_signal(
    bot: PromoBot,
    session: AsyncSession,
    *,
    posted_by_telegram_id: int | None = None,
    min_result_fixed_at: datetime | None = None,
) -> PromoPublishResult:
    settings = get_settings()
    if not settings.promo_results_chat_id:
        return PromoPublishResult(
            status="not_configured",
            message="Не настроен PROMO_RESULTS_CHAT_ID для публикации прошедших сигналов.",
        )

    query = (
        select(ScheduledSignal, Match, SignalResult)
        .join(Match, Match.id == ScheduledSignal.match_id)
        .join(SignalResult, SignalResult.signal_id == ScheduledSignal.id)
        .outerjoin(PromoSignalPost, PromoSignalPost.signal_id == ScheduledSignal.id)
        .where(ScheduledSignal.status == "sent")
        .where(SignalResult.status.in_(("won", "lost")))
        .where(PromoSignalPost.id.is_(None))
    )
    if min_result_fixed_at is not None:
        query = query.where(SignalResult.fixed_at >= min_result_fixed_at)
    row = (
        await session.execute(
            query.order_by(asc(Match.match_start_at), asc(ScheduledSignal.id)).limit(1)
        )
    ).first()

    if row is None:
        return PromoPublishResult(
            status="not_found",
            message="Нет сыгранных неопубликованных сигналов для промо-поста.",
        )

    signal, match, result = row
    text = format_promo_signal_post(signal, match, result)
    send_kwargs: dict[str, Any] = {
        "chat_id": settings.promo_results_chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if settings.promo_results_message_thread_id:
        send_kwargs["message_thread_id"] = settings.promo_results_message_thread_id

    try:
        sent_message = await bot.send_message(**send_kwargs)
    except Exception as exc:
        await session.rollback()
        return PromoPublishResult(
            status="error",
            message=f"Не удалось опубликовать прошедший сигнал #{signal.id}: {exc}",
            signal_id=signal.id,
        )

    telegram_message_id = getattr(sent_message, "message_id", None)
    session.add(
        PromoSignalPost(
            signal_id=signal.id,
            chat_id=settings.promo_results_chat_id,
            message_thread_id=settings.promo_results_message_thread_id or None,
            telegram_message_id=telegram_message_id,
            status="sent",
            posted_by_telegram_id=posted_by_telegram_id,
        )
    )
    await session.commit()

    return PromoPublishResult(
        status="sent",
        message=f"Опубликован прошедший сигнал #{signal.id} в промо-топик.",
        signal_id=signal.id,
        telegram_message_id=telegram_message_id,
    )
