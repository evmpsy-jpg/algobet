from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.settings import get_settings
from app.database.models import ScheduledSignal, SignalDelivery, User, UserAccess
from app.database.session import SessionFactory
from app.services.access import consume_signal_access, has_signal_access
from app.services.admin_notifications import format_delivery_failure_admin_text, notify_admins

logger = logging.getLogger(__name__)


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
    now = now or datetime.now().astimezone()
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
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка фоновой проверки сигналов")
        await asyncio.sleep(settings.scheduler_interval_seconds)
