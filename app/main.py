from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher

from app.settings import get_settings
from app.database.session import init_db
from app.handlers import admin, user
from app.services.backup_scheduler import sqlite_backup_loop
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

    background_tasks = [
        asyncio.create_task(signal_sender_loop(bot)),
        asyncio.create_task(sqlite_backup_loop(bot)),
    ]
    try:
        await dispatcher.start_polling(bot)
    finally:
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
