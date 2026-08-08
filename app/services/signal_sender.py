from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.settings import get_settings
from app.database.models import Match, ScheduledSignal, SignalDelivery, User, UserAccess
from app.database.session import SessionFactory
from app.services.access import consume_signal_access, has_signal_access
from app.services.admin_notifications import format_delivery_failure_admin_text, notify_admins
from app.services.excel_parser import ParsedMatch
from app.services.promo_publisher import publish_next_played_signal
from app.services.signal_results import auto_update_signal_results
from app.services.signal_rules import analyze_match, build_signal_message

logger = logging.getLogger(__name__)


def to_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


@dataclass(slots=True)
class SenderSummary:
    processed_signals: int = 0
    sent_deliveries: int = 0
    failed_deliveries: int = 0
    skipped_users: int = 0


async def _eligible_user_rows(
    session: AsyncSession,
    *,
    admin_ids: list[int],
    now: datetime,
    signal_payload: dict | None = None,
) -> list[tuple[User, UserAccess | None]]:
    rows = list(
        (
            await session.execute(
                select(User, UserAccess)
                .outerjoin(UserAccess, UserAccess.user_id == User.id)
                .where(User.is_active.is_(True))
            )
        ).all()
    )
    return [
        (user, access)
        for user, access in rows
        if has_signal_access(user, access, admin_ids=admin_ids, now=now, signal_payload=signal_payload)
    ]


async def _get_delivery(session: AsyncSession, signal: ScheduledSignal, user: User) -> SignalDelivery:
    delivery = await session.scalar(
        select(SignalDelivery).where(
            SignalDelivery.signal_id == signal.id,
            SignalDelivery.user_id == user.id,
        )
    )
    if delivery is None:
        delivery = SignalDelivery(
            signal_id=signal.id,
            user_id=user.id,
            telegram_id=user.telegram_id,
            status="pending",
        )
        session.add(delivery)
        await session.flush()
    return delivery



def _parsed_match_from_model(match: Match) -> ParsedMatch:
    return ParsedMatch(
        external_match_id=match.external_match_id,
        external_tournament_id=match.external_tournament_id,
        source_url=match.source_url,
        tournament_date=match.tournament_date,
        tournament_name=(match.raw_data or {}).get("_tournament_name") or "",
        match_time=match.match_time,
        match_start_at=match.match_start_at,
        player_1=match.player_1,
        player_2=match.player_2,
        player_1_rating=match.player_1_rating,
        player_2_rating=match.player_2_rating,
        score=match.score,
        raw_data=match.raw_data or {},
    )


async def _refresh_signal_from_current_match(session: AsyncSession, signal: ScheduledSignal) -> bool:
    match = await session.get(Match, signal.match_id)
    if match is None:
        signal.status = "cancelled"
        signal.cancel_reason = "Матч для сигнала не найден"
        return False
    if not match.raw_data:
        return True

    parsed = _parsed_match_from_model(match)
    decision = analyze_match(parsed)
    if not decision.suitable:
        signal.status = "cancelled"
        signal.cancel_reason = "Актуальные данные матча больше не подходят под правила"
        return False

    new_payload = decision.payload or {}
    old_payload = signal.signal_payload or {}
    new_message_text = build_signal_message(parsed, decision)
    if (
        old_payload.get("side") != new_payload.get("side")
        or old_payload.get("signal_group") != new_payload.get("signal_group")
        or old_payload.get("probability") != new_payload.get("probability")
        or signal.signal_type != decision.signal_type
        or signal.message_text != new_message_text
    ):
        signal.signal_payload = new_payload
        signal.signal_type = decision.signal_type
        signal.message_text = new_message_text
    return True

async def _process_signals(
    bot: Bot,
    session: AsyncSession,
    signals: list[ScheduledSignal],
    *,
    now_utc: datetime,
    admin_ids: list[int],
) -> SenderSummary:
    summary = SenderSummary()
    if not signals:
        return summary

    for signal in signals:
        summary.processed_signals += 1
        if signal.status == "cancelled":
            continue
        if signal.status != "sent":
            is_current = await _refresh_signal_from_current_match(session, signal)
            if not is_current:
                continue
        if not signal.message_text:
            signal.status = "cancelled"
            signal.cancel_reason = "Не сформирован текст сигнала"
            continue

        eligible_users = await _eligible_user_rows(
            session,
            admin_ids=admin_ids,
            now=now_utc,
            signal_payload=signal.signal_payload,
        )
        previous_status = signal.status
        signal.status = "ready"
        sent_for_signal = 0
        failed_for_signal = 0

        for user, access in eligible_users:
            if not has_signal_access(user, access, admin_ids=admin_ids, now=now_utc, signal_payload=signal.signal_payload):
                summary.skipped_users += 1
                continue
            delivery = await _get_delivery(session, signal, user)
            if delivery.status == "sent":
                summary.skipped_users += 1
                continue
            delivery.status = "pending"
            delivery.error_text = None
            try:
                await bot.send_message(
                    chat_id=user.telegram_id,
                    text=signal.message_text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception as exc:
                delivery.status = "failed"
                delivery.error_text = str(exc)[:1000]
                failed_for_signal += 1
                summary.failed_deliveries += 1
                logger.warning("Не удалось отправить сигнал %s пользователю %s: %s", signal.id, user.telegram_id, exc)
            else:
                delivery.status = "sent"
                delivery.sent_at = now_utc
                consume_signal_access(user, access, admin_ids=admin_ids, signal_payload=signal.signal_payload)
                sent_for_signal += 1
                summary.sent_deliveries += 1

        if sent_for_signal > 0:
            signal.status = "sent"
            signal.sent_at = now_utc
        elif previous_status == "sent":
            signal.status = "sent"
        elif failed_for_signal > 0:
            signal.status = "ready"
        else:
            signal.status = "ready"

    await session.commit()
    return summary


async def process_due_signals(
    bot: Bot,
    session: AsyncSession,
    *,
    now: datetime | None = None,
    admin_ids: list[int] | None = None,
) -> SenderSummary:
    now = to_utc_naive(now or datetime.now(timezone.utc))
    now_utc = datetime.utcnow()
    admin_ids = get_settings().admin_ids if admin_ids is None else admin_ids

    signals = list(
        (
            await session.scalars(
                select(ScheduledSignal)
                .where(ScheduledSignal.status.in_(["scheduled", "ready"]))
                .where(ScheduledSignal.send_at <= now)
                .order_by(ScheduledSignal.send_at.asc())
            )
        ).all()
    )
    return await _process_signals(bot, session, signals, now_utc=now_utc, admin_ids=admin_ids)


async def process_signal_now(
    bot: Bot,
    session: AsyncSession,
    signal_id: int,
    *,
    admin_ids: list[int] | None = None,
) -> SenderSummary:
    admin_ids = get_settings().admin_ids if admin_ids is None else admin_ids
    signal = await session.get(ScheduledSignal, signal_id)
    if signal is None:
        return SenderSummary()
    return await _process_signals(
        bot,
        session,
        [signal],
        now_utc=datetime.utcnow(),
        admin_ids=admin_ids,
    )


async def process_delivery_now(
    bot: Bot,
    session: AsyncSession,
    delivery_id: int,
    *,
    admin_ids: list[int] | None = None,
) -> SenderSummary:
    admin_ids = get_settings().admin_ids if admin_ids is None else admin_ids
    summary = SenderSummary()
    row = (
        await session.execute(
            select(SignalDelivery, ScheduledSignal, User, UserAccess)
            .join(ScheduledSignal, ScheduledSignal.id == SignalDelivery.signal_id)
            .join(User, User.id == SignalDelivery.user_id)
            .outerjoin(UserAccess, UserAccess.user_id == User.id)
            .where(SignalDelivery.id == delivery_id)
        )
    ).first()
    if row is None:
        return summary

    delivery, signal, user, access = row
    summary.processed_signals = 1
    if delivery.status == "sent":
        summary.skipped_users = 1
        return summary
    now_utc = datetime.utcnow()
    if not has_signal_access(user, access, admin_ids=admin_ids, now=now_utc, signal_payload=signal.signal_payload):
        summary.skipped_users = 1
        return summary

    delivery.status = "pending"
    delivery.error_text = None
    try:
        await bot.send_message(
            chat_id=user.telegram_id,
            text=signal.message_text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as exc:
        delivery.status = "failed"
        delivery.error_text = str(exc)[:1000]
        summary.failed_deliveries = 1
        logger.warning("Не удалось повторно отправить доставку %s пользователю %s: %s", delivery.id, user.telegram_id, exc)
    else:
        delivery.status = "sent"
        delivery.sent_at = now_utc
        signal.status = "sent"
        signal.sent_at = signal.sent_at or now_utc
        consume_signal_access(user, access, admin_ids=admin_ids, signal_payload=signal.signal_payload)
        summary.sent_deliveries = 1
    await session.commit()
    return summary


def _parse_promo_auto_start_at(value: str | None) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Некорректный PROMO_RESULTS_AUTO_START_AT: %s", raw)
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


async def signal_sender_loop(bot: Bot) -> None:
    settings = get_settings()
    while True:
        try:
            async with SessionFactory() as session:
                summary = await process_due_signals(bot, session)
                if summary.processed_signals:
                    logger.info(
                        "Проверка сигналов: processed=%s sent=%s failed=%s skipped=%s",
                        summary.processed_signals,
                        summary.sent_deliveries,
                        summary.failed_deliveries,
                        summary.skipped_users,
                    )
                    if summary.failed_deliveries:
                        await notify_admins(
                            bot,
                            settings.admin_ids,
                            format_delivery_failure_admin_text(
                                summary.processed_signals,
                                summary.failed_deliveries,
                                summary.sent_deliveries,
                            ),
                        )

                result_summary = await auto_update_signal_results(session)
                if result_summary.updated:
                    await session.commit()
                    logger.info(
                        "Автообновление результатов: scanned=%s updated=%s no_score=%s",
                        result_summary.scanned,
                        result_summary.updated,
                        result_summary.no_score,
                    )

                if settings.promo_results_auto_enabled:
                    lookback_hours = max(1, int(settings.promo_results_auto_lookback_hours or 1))
                    promo_result = await publish_next_played_signal(
                        bot,
                        session,
                        min_result_fixed_at=datetime.utcnow() - timedelta(hours=lookback_hours),
                        min_match_start_at=_parse_promo_auto_start_at(settings.promo_results_auto_start_at),
                    )
                    if promo_result.status == "sent":
                        logger.info("Промо-сигнал опубликован автоматически: signal_id=%s", promo_result.signal_id)
                    elif promo_result.status == "error":
                        logger.warning("Ошибка автопубликации промо-сигнала: %s", promo_result.message)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка фоновой проверки сигналов")
        await asyncio.sleep(settings.scheduler_interval_seconds)
