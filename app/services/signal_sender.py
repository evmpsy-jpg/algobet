from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from aiogram import Bot
from sqlalchemy import select

from app.settings import get_settings
from app.database.models import ScheduledSignal, User
from app.database.session import SessionFactory

logger = logging.getLogger(__name__)


async def signal_sender_loop(bot: Bot) -> None:
    settings = get_settings()
    while True:
        try:
            async with SessionFactory() as session:
                now = datetime.now().astimezone()
                signals = list(
                    (
                        await session.scalars(
                            select(ScheduledSignal).where(
                                ScheduledSignal.status == "scheduled",
                                ScheduledSignal.send_at <= now,
                            )
                        )
                    ).all()
                )
                if signals:
                    users = list(
                        (await session.scalars(select(User).where(User.is_active.is_(True)))).all()
                    )
                    for signal in signals:
                        # В MVP отправка всем активным пользователям временно отключена,
                        # пока не реализованы подписки и тестовые лимиты.
                        if not signal.message_text:
                            signal.status = "cancelled"
                            signal.cancel_reason = "Не сформирован текст сигнала"
                            continue
                        logger.info(
                            "Сигнал %s готов к выдаче %s пользователям, но доступы ещё не настроены",
                            signal.id,
                            len(users),
                        )
                        signal.status = "ready"
                    await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка фоновой проверки сигналов")
        await asyncio.sleep(settings.scheduler_interval_seconds)
