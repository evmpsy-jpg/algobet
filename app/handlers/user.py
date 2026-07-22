from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import desc, func, select

from app.settings import get_settings
from app.database.models import ImportBatch, Match, ScheduledSignal, SignalResult, User, UserAccess
from app.database.session import SessionFactory
from app.keyboards.common import main_menu
from app.services.access import has_signal_access, ensure_trial_access
from app.services.signal_results import format_winrate, result_short_label, summarize_results
from app.services.stake_calculator import STEP_OPTIONS, format_step_stake_calculator, parse_bank, parse_step

router = Router(name="user")


class CalculatorStates(StatesGroup):
    waiting_for_bank = State()
    waiting_for_step = State()

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

def _local_dt(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(ZoneInfo(get_settings().timezone))


def _fmt_dt(value: datetime | None, fmt: str = "%d.%m %H:%M") -> str:
    local = _local_dt(value)
    return local.strftime(fmt) if local else "—"


def format_public_results(
    rows: list[tuple[ScheduledSignal, Match, SignalResult]],
    total_sent: int,
) -> str:
    summary = summarize_results(
        [(signal.signal_payload, result.status) for signal, _, result in rows],
        total_sent=total_sent,
    )
    lines = [
        "🏆 Результаты сигналов",
        "",
        f"Оценено: {summary.evaluated} из {summary.total_sent}",
        f"✅ Зашло: {summary.overall.won}",
        f"❌ Не зашло: {summary.overall.lost}",
        f"↩️ Возврат: {summary.overall.void}",
        f"Winrate: {format_winrate(summary.overall.winrate)}",
        "",
        "Последние результаты:",
    ]
    if not rows:
        lines.append("пока нет оцененных сигналов")
        return "\n".join(lines)

    for signal, match, result in rows[:10]:
        payload = signal.signal_payload or {}
        side = payload.get("side")
        side_text = f"П{side}" if side in (1, 2) else "—"
        level = payload.get("level") or "—"
        sent_at = _fmt_dt(signal.sent_at or signal.send_at)
        lines.extend([
            f"• {sent_at} · {result_short_label(result.status)} · {level} · {side_text}",
            f"  {match.player_1} — {match.player_2}",
        ])
        if match.score:
            lines.append(f"  счёт: {match.score}")
    return "\n".join(lines)[:3900]



def format_subscription_status(user: User | None, access: UserAccess | None, *, is_admin: bool = False) -> str:
    lines = ["💳 Подписка", ""]
    if is_admin:
        lines.extend([
            "У вас админ-доступ.",
            "Сигналы доступны без ограничений trial/paid.",
        ])
        return "\n".join(lines)

    if user is None or access is None:
        lines.extend([
            "Активного доступа пока нет.",
            "Нажмите 🎁 Первые 3 сигнала, чтобы активировать пробный доступ.",
        ])
        return "\n".join(lines)

    active = has_signal_access(user, access, admin_ids=[])
    status_text = "активна" if active else "не активна"
    lines.append(f"Статус: {status_text}")
    if access.access_type == "trial":
        lines.extend([
            "Тип: пробный доступ",
            f"Осталось бесплатных сигналов: {access.free_signals_remaining}",
        ])
        if access.free_signals_remaining <= 0:
            lines.append("Пробные сигналы закончились. Для продления напишите администратору.")
    elif access.access_type == "paid":
        until = _fmt_dt(access.active_until, "%d.%m.%Y") if access.active_until else "без даты окончания"
        lines.extend([
            "Тип: платный доступ",
            f"Активен до: {until}",
        ])
        if not active:
            lines.append("Срок доступа истёк. Для продления напишите администратору.")
    else:
        lines.append(f"Тип: {access.access_type}")
    if access.status != "active":
        lines.append("Доступ отключён. Для включения напишите администратору.")
    return "\n".join(lines)



def format_tournament_analytics(
    *,
    latest_import: ImportBatch | None,
    total_matches: int,
    active_matches: int,
    scheduled_signals: int,
    ready_signals: int,
    upcoming_matches: list[Match],
    upcoming_signals: list[tuple[ScheduledSignal, Match]],
) -> str:
    lines = ["📊 Аналитика турниров", ""]
    if latest_import is None:
        lines.append("Данные турниров пока не загружены.")
        return "\n".join(lines)

    lines.extend([
        f"Последняя загрузка: {_fmt_dt(latest_import.finished_at or latest_import.created_at, '%d.%m %H:%M')}",
        f"Матчей в базе: {total_matches}",
        f"Актуальных матчей: {active_matches}",
        f"Запланированных сигналов: {scheduled_signals}",
        f"Готовых к отправке: {ready_signals}",
        "",
        "Ближайшие матчи:",
    ])
    if upcoming_matches:
        for match in upcoming_matches[:5]:
            lines.append(f"• {_fmt_dt(match.match_start_at)} · {match.player_1} — {match.player_2}")
    else:
        lines.append("пока нет ближайших матчей")

    lines.extend(["", "Ближайшие сигналы:"])
    if upcoming_signals:
        for signal, match in upcoming_signals[:5]:
            payload = signal.signal_payload or {}
            side = payload.get("side")
            side_text = f"П{side}" if side in (1, 2) else "—"
            level = payload.get("level") or "—"
            lines.append(f"• {_fmt_dt(signal.send_at)} · {level} · {side_text} · {match.player_1} — {match.player_2}")
    else:
        lines.append("пока нет ближайших сигналов")
    return "\n".join(lines)[:3900]


@router.message(F.text == "📊 Аналитика турниров")
async def tournament_analytics_handler(message: Message) -> None:
    now = datetime.utcnow()
    async with SessionFactory() as session:
        latest_import = await session.scalar(select(ImportBatch).order_by(desc(ImportBatch.id)).limit(1))
        total_matches = int(await session.scalar(select(func.count(Match.id))) or 0)
        active_matches = int(await session.scalar(select(func.count(Match.id)).where(Match.is_present_in_latest_import.is_(True))) or 0)
        scheduled_signals = int(await session.scalar(select(func.count(ScheduledSignal.id)).where(ScheduledSignal.status == "scheduled")) or 0)
        ready_signals = int(await session.scalar(select(func.count(ScheduledSignal.id)).where(ScheduledSignal.status == "ready")) or 0)
        upcoming_matches = list((await session.scalars(
            select(Match)
            .where(Match.is_present_in_latest_import.is_(True))
            .where(Match.match_start_at >= now)
            .order_by(Match.match_start_at.asc())
            .limit(5)
        )).all())
        upcoming_signals = list((await session.execute(
            select(ScheduledSignal, Match)
            .join(Match, Match.id == ScheduledSignal.match_id)
            .where(ScheduledSignal.status.in_(["scheduled", "ready"]))
            .order_by(ScheduledSignal.send_at.asc())
            .limit(5)
        )).all())
    await message.answer(format_tournament_analytics(
        latest_import=latest_import,
        total_matches=total_matches,
        active_matches=active_matches,
        scheduled_signals=scheduled_signals,
        ready_signals=ready_signals,
        upcoming_matches=upcoming_matches,
        upcoming_signals=upcoming_signals,
    ))

@router.message(F.text == "💳 Подписка")
async def subscription_handler(message: Message) -> None:
    if message.from_user is None:
        return
    is_admin = message.from_user.id in get_settings().admin_ids
    async with SessionFactory() as session:
        row = (await session.execute(
            select(User, UserAccess)
            .outerjoin(UserAccess, UserAccess.user_id == User.id)
            .where(User.telegram_id == message.from_user.id)
        )).first()
    if row is None:
        await message.answer(format_subscription_status(None, None, is_admin=is_admin))
        return
    user, access = row
    await message.answer(format_subscription_status(user, access, is_admin=is_admin))


def format_help_information() -> str:
    return "\n".join([
        "📚 Полезная информация",
        "",
        "Как работает бот:",
        "• администратор загружает Excel с матчами турниров;",
        "• правило отбора формирует сигналы заранее;",
        "• доступные сигналы отправляются пользователям с активным доступом;",
        "• результаты фиксируются вручную или автоматически по счету из новой загрузки.",
        "",
        "Где что смотреть:",
        "• 📊 Аналитика турниров — свежая загрузка, ближайшие матчи и сигналы;",
        "• 💳 Подписка — ваш текущий доступ;",
        "• 🏆 Результаты — последние оцененные сигналы и winrate;",
        "• 🎁 Первые 3 сигнала — активация пробного доступа.",
        "",
        "Важно: сигналы не являются гарантией результата. Используйте их как аналитическую подсказку и контролируйте риск.",
    ])


@router.message(F.text == "📚 Полезная информация")
async def help_information_handler(message: Message) -> None:
    await message.answer(format_help_information())

@router.message(F.text == "🏆 Результаты")
async def public_results_handler(message: Message) -> None:
    async with SessionFactory() as session:
        total_sent = int(await session.scalar(
            select(func.count(ScheduledSignal.id)).where(ScheduledSignal.status == "sent")
        ) or 0)
        rows = list((await session.execute(
            select(ScheduledSignal, Match, SignalResult)
            .join(Match, Match.id == ScheduledSignal.match_id)
            .join(SignalResult, SignalResult.signal_id == ScheduledSignal.id)
            .where(SignalResult.status.in_(["won", "lost", "void"]))
            .order_by(desc(SignalResult.fixed_at), desc(ScheduledSignal.sent_at), desc(ScheduledSignal.id))
            .limit(10)
        )).all())
    await message.answer(format_public_results(rows, total_sent))

def calculator_result_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔁 Сделать ещё расчёт", callback_data="calc:again")],
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="calc:menu")],
    ])

def calculator_step_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"1/{step}", callback_data=f"calc:step:{step}") for step in STEP_OPTIONS[:3]],
        [InlineKeyboardButton(text=f"1/{step}", callback_data=f"calc:step:{step}") for step in STEP_OPTIONS[3:]],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(F.text == "🧮 Калькулятор")
async def calculator_handler(message: Message, state: FSMContext) -> None:
    await state.set_state(CalculatorStates.waiting_for_bank)
    await message.answer("🧮 Какой у вас банк?\n\nВведите сумму числом. Например: 5500")


@router.message(CalculatorStates.waiting_for_bank)
async def calculator_bank_handler(message: Message, state: FSMContext) -> None:
    try:
        bank = parse_bank(message.text or "")
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(bank=str(bank))
    await state.set_state(CalculatorStates.waiting_for_step)
    await message.answer("Какой шаг выбираете?", reply_markup=calculator_step_keyboard())


@router.callback_query(CalculatorStates.waiting_for_step, F.data.startswith("calc:step:"))
async def calculator_step_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.data is None or callback.message is None:
        return
    try:
        step = parse_step(callback.data.split(":")[-1])
        data = await state.get_data()
        bank = parse_bank(str(data.get("bank") or ""))
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(format_step_stake_calculator(bank, step), reply_markup=calculator_result_keyboard())
    await callback.answer()

@router.callback_query(F.data == "calc:again")
async def calculator_again_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        return
    await state.set_state(CalculatorStates.waiting_for_bank)
    await callback.message.edit_text("🧮 Какой у вас банк?\n\nВведите сумму числом. Например: 5500")
    await callback.answer()


@router.callback_query(F.data == "calc:menu")
async def calculator_menu_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        return
    await state.clear()
    is_admin = bool(callback.from_user and callback.from_user.id in get_settings().admin_ids)
    await callback.message.answer("Главное меню", reply_markup=main_menu(is_admin=is_admin))
    await callback.answer()

@router.message(F.text.in_({
    "📚 Полезная информация",
    "🔎 Анализ матча",
}))
async def placeholder_handler(message: Message) -> None:
    await message.answer("Раздел подготовлен в меню и будет подключён на следующих этапах.")
