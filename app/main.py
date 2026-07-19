from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher

from app.config import get_settings
from app.database.session import init_db
from app.handlers import admin, user
from app.services.signal_sender import signal_sender_loop


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    settings = get_settings()
    await init_db()

    bot = Bot(token=settings.bot_token)
    dispatcher = Dispatcher()
    dispatcher.include_router(admin.router)
    dispatcher.include_router(user.router)

    sender_task = asyncio.create_task(signal_sender_loop(bot))
    try:
        await dispatcher.start_polling(bot)
    finally:
        sender_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
