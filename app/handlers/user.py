from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import Message
from sqlalchemy import select

from app.settings import get_settings
from app.database.models import User
from app.database.session import SessionFactory
from app.keyboards.common import main_menu
from app.services.access import ensure_trial_access

router = Router(name="user")


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    if message.from_user is None:
        return
    async with SessionFactory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
        if user is None:
            user = User(
                telegram_id=message.from_user.id,
                username=message.from_user.username,
                first_name=message.from_user.first_name,
                last_name=message.from_user.last_name,
            )
            session.add(user)
        else:
            user.username = message.from_user.username
            user.first_name = message.from_user.first_name
            user.last_name = message.from_user.last_name
            user.is_active = True
        await session.commit()

    is_admin = message.from_user.id in get_settings().admin_ids
    await message.answer(
        "Добро пожаловать в АлгоБет.\n\nСейчас запущен первый технический этап бота.",
        reply_markup=main_menu(is_admin=is_admin),
    )


@router.message(F.text == "⬅️ Главное меню")
async def back_to_menu(message: Message) -> None:
    is_admin = bool(message.from_user and message.from_user.id in get_settings().admin_ids)
    await message.answer("Главное меню", reply_markup=main_menu(is_admin=is_admin))




@router.message(F.text == "🎁 Первые 3 сигнала")
async def trial_signals_handler(message: Message) -> None:
    if message.from_user is None:
        return
    async with SessionFactory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
        if user is None:
            user = User(
                telegram_id=message.from_user.id,
                username=message.from_user.username,
                first_name=message.from_user.first_name,
                last_name=message.from_user.last_name,
            )
            session.add(user)
            await session.flush()
        access = await ensure_trial_access(session, user)
        await session.commit()
    await message.answer(
        "🎁 Пробный доступ активен.\n\n"
        f"Осталось бесплатных сигналов: {access.free_signals_remaining}."
    )
@router.message(F.text.in_({
    "📊 Аналитика турниров",
    "💳 Подписка",
    "📚 Полезная информация",
    "🏆 Результаты",
    "🔎 Анализ матча",
    "🧮 Калькулятор",
}))
async def placeholder_handler(message: Message) -> None:
    await message.answer("Раздел подготовлен в меню и будет подключён на следующих этапах.")
