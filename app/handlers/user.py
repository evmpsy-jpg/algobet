from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import desc, func, select

from app.settings import get_settings
from app.database.models import ImportBatch, Match, ScheduledSignal, SignalDecisionLog, SignalResult, User, UserAccess
from app.database.session import SessionFactory
from app.keyboards.common import main_menu
from app.services.access import has_analytics_access, has_signal_access, ensure_trial_access
from app.services.admin_notifications import notify_admins
from app.services.bot_settings import get_analysis_payment_config
from app.services.match_analysis import (
    create_match_analysis_request,
    format_analysis_payment_admin_text,
    format_analysis_payment_text,
    format_analysis_request_admin_text,
    format_analysis_request_user_text,
    format_upcoming_matches_text,
    get_upcoming_matches,
)
from app.services.signal_results import format_winrate, result_short_label, signal_stats_eligible, summarize_results
from app.services.stake_calculator import STEP_OPTIONS, format_step_stake_calculator, parse_bank, parse_step
from app.services.subscriptions import (
    PLAN_GROUP_LABELS,
    SUBSCRIPTION_PLANS,
    create_subscription_request,
    format_subscription_plan_line,
    format_subscription_plans_text,
    format_subscription_request_admin_text,
    format_subscription_request_user_text,
)

router = Router(name="user")


class CalculatorStates(StatesGroup):
    waiting_for_bank = State()
    waiting_for_step = State()



WELCOME_MESSAGES = (
    (
        "Вас приветствует бот Алгобет.\n\n"
        "Алгоритмический беттинг по настольному теннису Лиги Про."
    ),
    (
        "Здесь вы можете получать сигналы на сет, смотреть аналитику турниров 24/7, "
        "рассчитать размер ставки от банка и оставить заявку на разбор матча специалистом."
    ),
)


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
    for text in WELCOME_MESSAGES:
        await message.answer(text)
    await message.answer("Выберите нужный раздел в меню.", reply_markup=main_menu(is_admin=is_admin))


@router.message(F.text == "⬅️ Главное меню")
async def back_to_menu(message: Message) -> None:
    is_admin = bool(message.from_user and message.from_user.id in get_settings().admin_ids)
    await message.answer("Главное меню", reply_markup=main_menu(is_admin=is_admin))




@router.message(F.text == "🎁 Первые 9 сигналов")
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
    rows: list[tuple],
    total_sent: int,
) -> str:
    visible_rows: list[tuple[ScheduledSignal, Match, SignalResult]] = []
    for row in rows:
        if len(row) == 4:
            signal, match, result, decision_suitable = row
        else:
            signal, match, result = row
            decision_suitable = None
        if signal_stats_eligible(match, decision_suitable=decision_suitable):
            visible_rows.append((signal, match, result))
    summary = summarize_results(
        [(signal.signal_payload, result.status) for signal, _, result in visible_rows],
        total_sent=min(total_sent, len(visible_rows)),
    )
    correction = {
        "won": max(int(get_settings().stats_correction_won), 0),
        "lost": max(int(get_settings().stats_correction_lost), 0),
        "void": max(int(get_settings().stats_correction_void), 0),
        "unknown": max(int(get_settings().stats_correction_unknown), 0),
    }
    correction_total = sum(correction.values())
    official_won = max(summary.overall.won - correction["won"], 0)
    official_lost = max(summary.overall.lost - correction["lost"], 0)
    official_void = max(summary.overall.void - correction["void"], 0)
    official_unknown = max(summary.overall.unknown - correction["unknown"], 0)
    official_total = max(summary.total_sent - correction_total, 0)
    official_evaluated = official_won + official_lost + official_void + official_unknown
    if official_evaluated > official_total:
        official_total = official_evaluated
    official_winrate = None
    if official_won + official_lost:
        official_winrate = official_won / (official_won + official_lost) * 100
    lines = [
        "🏆 Результаты сигналов",
        "",
        f"Оценено: {official_evaluated} из {official_total}",
        f"✅ Зашло: {official_won}",
        f"❌ Не зашло: {official_lost}",
        f"↩️ Возврат: {official_void}",
        f"Процент захода: {format_winrate(official_winrate)}",
        "",
        "Последние результаты:",
    ]
    if not visible_rows:
        lines.append("пока нет оцененных сигналов")
        return "\n".join(lines)

    for signal, match, result in visible_rows[:10]:
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



def subscription_plans_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for group, label in PLAN_GROUP_LABELS.items():
        rows.append([InlineKeyboardButton(text=label, callback_data=f"sub:group:{group}")])
    rows.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data="sub:main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subscription_group_keyboard(group: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for plan in SUBSCRIPTION_PLANS:
        if plan.group != group:
            continue
        rows.append([
            InlineKeyboardButton(
                text=f"{plan.description} · {plan.price_rub:,}р".replace(",", " "),
                callback_data=f"sub:plan:{plan.id}",
            )
        ])
    rows.append([InlineKeyboardButton(text="⬅️ К тарифам", callback_data="sub:plans")])
    rows.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data="sub:main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_subscription_group_text(group: str) -> str:
    title = PLAN_GROUP_LABELS.get(group, "Тарифы")
    lines = [f"💳 {title}", ""]
    for plan in SUBSCRIPTION_PLANS:
        if plan.group == group:
            lines.append(f"• {format_subscription_plan_line(plan)}")
    lines.extend(["", "Выберите подходящий вариант."])
    return "\n".join(lines)


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
            "Нажмите 🎁 Первые 9 сигналов, чтобы активировать пробный доступ.",
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
        plan = next((item for item in SUBSCRIPTION_PLANS if item.id == access.plan_id), None)
        plan_text = format_subscription_plan_line(plan) if plan else "платный доступ"
        remaining = "без лимита" if access.signals_remaining is None else str(access.signals_remaining)
        lines.extend([
            "Тип: платный доступ",
            f"Тариф: {plan_text}",
            f"Осталось сигналов: {remaining}",
            f"Активен до: {until}",
        ])
        if access.includes_analytics:
            lines.append("Аналитика турниров: доступна")
        if not active:
            lines.append("Срок или лимит доступа закончился. Для продления выберите тариф ниже.")
    else:
        lines.append(f"Тип: {access.access_type}")
    if access.status != "active":
        lines.append("Доступ отключён. Для включения напишите администратору.")
    return "\n".join(lines)



def filter_accessible_signal_rows(
    user: User,
    access: UserAccess | None,
    signal_rows: list[tuple[ScheduledSignal, Match]],
    *,
    admin_ids: list[int],
    now: datetime,
) -> list[tuple[ScheduledSignal, Match]]:
    return [
        (signal, match)
        for signal, match in signal_rows
        if has_signal_access(
            user,
            access,
            admin_ids=admin_ids,
            now=now,
            signal_payload=signal.signal_payload,
        )
    ]

def format_tournament_analytics(
    *,
    latest_import: ImportBatch | None,
    total_matches: int,
    active_matches: int,
    scheduled_signals: int,
    ready_signals: int,
    upcoming_signals: list[tuple[ScheduledSignal, Match]],
) -> str:
    visible_signals = [
        (signal, match)
        for signal, match in upcoming_signals
        if signal_stats_eligible(match)
    ]
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
        "Ближайшие сигналы:",
    ])
    if visible_signals:
        for signal, match in visible_signals:
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
    if message.from_user is None:
        return
    now = datetime.utcnow()
    settings = get_settings()
    async with SessionFactory() as session:
        row = (await session.execute(
            select(User, UserAccess)
            .outerjoin(UserAccess, UserAccess.user_id == User.id)
            .where(User.telegram_id == message.from_user.id)
        )).first()
        if row is None or not has_analytics_access(row[0], row[1], admin_ids=settings.admin_ids, now=now):
            await message.answer(
                "📊 Аналитика турниров доступна в тарифе «Всё включено».\n\n"
                "Откройте 💳 Подписка и выберите подходящий вариант."
            )
            return
        latest_import = await session.scalar(select(ImportBatch).order_by(desc(ImportBatch.id)).limit(1))
        total_matches = int(await session.scalar(select(func.count(Match.id))) or 0)
        active_matches = int(await session.scalar(select(func.count(Match.id)).where(Match.is_present_in_latest_import.is_(True))) or 0)
        scheduled_signals = int(await session.scalar(select(func.count(ScheduledSignal.id)).where(ScheduledSignal.status == "scheduled")) or 0)
        ready_signals = int(await session.scalar(select(func.count(ScheduledSignal.id)).where(ScheduledSignal.status == "ready")) or 0)
        user, access = row
        local_now = now.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(settings.timezone))
        day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        day_start_utc = day_start.astimezone(timezone.utc).replace(tzinfo=None)
        day_end_utc = day_end.astimezone(timezone.utc).replace(tzinfo=None)
        signal_rows = list((await session.execute(
            select(ScheduledSignal, Match, SignalDecisionLog.suitable)
            .join(Match, Match.id == ScheduledSignal.match_id)
            .outerjoin(
                SignalDecisionLog,
                (SignalDecisionLog.match_id == ScheduledSignal.match_id)
                & (SignalDecisionLog.import_batch_id == ScheduledSignal.source_import_id),
            )
            .where(ScheduledSignal.status.in_(["scheduled", "ready"]))
            .where(ScheduledSignal.send_at >= now)
            .where(ScheduledSignal.send_at >= day_start_utc)
            .where(ScheduledSignal.send_at < day_end_utc)
            .order_by(ScheduledSignal.send_at.asc())
        )).all())
        upcoming_signals = filter_accessible_signal_rows(
            user,
            access,
            [(signal, match) for signal, match, decision_suitable in signal_rows if signal_stats_eligible(match, decision_suitable=decision_suitable)],
            admin_ids=settings.admin_ids,
            now=now,
        )
        visible_upcoming_signals = upcoming_signals
        scheduled_signals = sum(1 for signal, match in visible_upcoming_signals if signal.status == "scheduled")
        ready_signals = sum(1 for signal, match in visible_upcoming_signals if signal.status == "ready")
    await message.answer(format_tournament_analytics(
        latest_import=latest_import,
        total_matches=total_matches,
        active_matches=active_matches,
        scheduled_signals=scheduled_signals,
        ready_signals=ready_signals,
        upcoming_signals=visible_upcoming_signals,
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
    else:
        user, access = row
        await message.answer(format_subscription_status(user, access, is_admin=is_admin))
    await message.answer(format_subscription_plans_text(), reply_markup=subscription_plans_keyboard())

@router.callback_query(F.data == "sub:plans")
async def subscription_plans_callback(callback: CallbackQuery) -> None:
    if callback.message:
        await callback.message.edit_text(format_subscription_plans_text(), reply_markup=subscription_plans_keyboard())
    await callback.answer()


@router.callback_query(F.data == "sub:main_menu")
async def subscription_main_menu_callback(callback: CallbackQuery) -> None:
    is_admin = bool(callback.from_user and callback.from_user.id in get_settings().admin_ids)
    if callback.message:
        await callback.message.answer("Главное меню", reply_markup=main_menu(is_admin=is_admin))
    await callback.answer()


@router.callback_query(F.data.startswith("sub:group:"))
async def subscription_group_callback(callback: CallbackQuery) -> None:
    if not callback.data:
        return
    _, _, group = callback.data.split(":")
    if group not in PLAN_GROUP_LABELS:
        await callback.answer("Раздел не найден", show_alert=True)
        return
    if callback.message:
        await callback.message.edit_text(format_subscription_group_text(group), reply_markup=subscription_group_keyboard(group))
    await callback.answer()


@router.callback_query(F.data.startswith("sub:plan:"))
async def subscription_plan_callback(callback: CallbackQuery) -> None:
    if callback.from_user is None or not callback.data:
        return
    _, _, plan_id = callback.data.split(":")
    settings = get_settings()
    async with SessionFactory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
        if user is None:
            user = User(
                telegram_id=callback.from_user.id,
                username=callback.from_user.username,
                first_name=callback.from_user.first_name,
                last_name=callback.from_user.last_name,
            )
            session.add(user)
            await session.flush()
        else:
            user.username = callback.from_user.username
            user.first_name = callback.from_user.first_name
            user.last_name = callback.from_user.last_name
            user.is_active = True
        try:
            request = await create_subscription_request(session, user, plan_id)
        except ValueError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        user_text = format_subscription_request_user_text(request)
        admin_text = format_subscription_request_admin_text(request, user)
        await session.commit()

    if callback.message:
        await callback.message.edit_text(user_text)
    await notify_admins(callback.bot, settings.admin_ids, admin_text)
    await callback.answer("Заявка создана", show_alert=True)



def format_help_information() -> str:
    return "\n".join([
        "📚 Полезная информация",
        "",
        "Как работает бот:",
        "",
        "• Бот сканирует уникальную фильтр таблицу Excel с матчами турниров по настольному теннису Лиги Про;",
        "",
        "• Учитывая статистику проходных критериев, формируются сигналы на сет;",
        "",
        "• Сигналы делятся на 2 категории:",
        "1. VIP ( 99%) на дистанции",
        "2. Все сигналы, включая  VIP ( 95%)",
        "",
        "• автоматически за 10 мин до начала события сигналы отправляются пользователям с активным доступом;",
        "",
        "• результаты обновляются автоматически.",
        "",
        "Где что смотреть:",
        "",
        "• 📊 Аналитика турниров — свежая загрузка и доступные сигналы на сегодня;",
        "• 💳 Подписка — ваш текущий доступ;",
        "• 🏆 Результаты — последние оцененные сигналы;",
        "• 🎁 Первые 9 сигналов — активация пробного доступа.",
        "",
        "Важно: сигналы не являются гарантией результата. Используйте их как аналитическую подсказку и контролируйте риск.",
    ])


@router.message(F.text == "📚 Полезная информация")
async def help_information_handler(message: Message) -> None:
    await message.answer(format_help_information())

@router.message(F.text == "🏆 Результаты")
async def public_results_handler(message: Message) -> None:
    async with SessionFactory() as session:
        rows = list((await session.execute(
            select(ScheduledSignal, Match, SignalResult, SignalDecisionLog.suitable)
            .join(Match, Match.id == ScheduledSignal.match_id)
            .join(SignalResult, SignalResult.signal_id == ScheduledSignal.id)
            .outerjoin(
                SignalDecisionLog,
                (SignalDecisionLog.match_id == ScheduledSignal.match_id)
                & (SignalDecisionLog.import_batch_id == ScheduledSignal.source_import_id),
            )
            .where(SignalResult.status.in_(["won", "lost", "void"]))
            .order_by(desc(SignalResult.fixed_at), desc(ScheduledSignal.sent_at), desc(ScheduledSignal.id))
        )).all())
    await message.answer(format_public_results(rows, len(rows)))

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


def analysis_matches_keyboard(matches: list[Match]) -> InlineKeyboardMarkup:
    rows = []
    for match in matches:
        time_text = _fmt_dt(match.match_start_at, '%H:%M')
        button_text = f"{time_text} · {match.player_1} — {match.player_2}"
        rows.append([InlineKeyboardButton(text=button_text[:64], callback_data=f"an:pick:{match.id}")])
    if matches:
        rows.append([InlineKeyboardButton(text="⬅️ Вернуться в главное меню", callback_data="an:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def analysis_payment_keyboard(request_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"an:paid:{request_id}")],
        [InlineKeyboardButton(text="⬅️ Вернуться в главное меню", callback_data="an:menu")],
    ])


def analysis_issue_keyboard(request_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Выдать анализ клиенту", callback_data=f"an:issue:{request_id}")],
    ])


@router.message(F.text == "🔎 Анализ матча")
async def match_analysis_handler(message: Message) -> None:
    if message.from_user is None:
        return
    async with SessionFactory() as session:
        matches = await get_upcoming_matches(session, limit=10)
    await message.answer(format_upcoming_matches_text(matches), reply_markup=analysis_matches_keyboard(matches))


@router.callback_query(F.data == "an:menu")
async def analysis_menu_callback(callback: CallbackQuery) -> None:
    is_admin = bool(callback.from_user and callback.from_user.id in get_settings().admin_ids)
    if callback.message:
        await callback.message.answer("Главное меню", reply_markup=main_menu(is_admin=is_admin))
    await callback.answer()


@router.callback_query(F.data.startswith("an:pick:"))
async def analysis_pick_callback(callback: CallbackQuery) -> None:
    if callback.from_user is None or callback.data is None:
        return
    _, _, match_id_raw = callback.data.split(":")
    settings = get_settings()
    async with SessionFactory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
        if user is None:
            user = User(
                telegram_id=callback.from_user.id,
                username=callback.from_user.username,
                first_name=callback.from_user.first_name,
                last_name=callback.from_user.last_name,
            )
            session.add(user)
            await session.flush()
        else:
            user.username = callback.from_user.username
            user.first_name = callback.from_user.first_name
            user.last_name = callback.from_user.last_name
            user.is_active = True

        match = await session.get(Match, int(match_id_raw))
        if match is None:
            await callback.answer("Матч не найден", show_alert=True)
            return

        payment_config = await get_analysis_payment_config(session)
        request = await create_match_analysis_request(
            session,
            user,
            match,
            payment_details=payment_config.payment_details,
            specialist_contact=payment_config.specialist_contact,
        )
        user_text = format_analysis_request_user_text(request)
        admin_text = format_analysis_request_admin_text(request, user)
        await session.commit()

    if callback.message:
        await callback.message.edit_text(user_text, reply_markup=analysis_payment_keyboard(request.id))
    await notify_admins(callback.bot, settings.admin_ids, admin_text)
    await callback.answer("Заявка создана", show_alert=True)


@router.callback_query(F.data.startswith("an:paid:"))
async def analysis_paid_callback(callback: CallbackQuery) -> None:
    if callback.from_user is None or callback.data is None:
        return
    _, _, request_id_raw = callback.data.split(":")
    request_id = int(request_id_raw)
    settings = get_settings()
    async with SessionFactory() as session:
        request = await session.get(MatchAnalysisRequest, request_id)
        if request is None:
            await callback.answer("Заявка не найдена", show_alert=True)
            return
        if request.telegram_id != callback.from_user.id:
            await callback.answer("Вы уже оплатили", show_alert=True)
            return
        user = await session.scalar(select(User).where(User.id == request.user_id))
        if user is None:
            await callback.answer("Пользователь не найден", show_alert=True)
            return
        request.status = "paid"
        request.updated_at = datetime.utcnow()
        admin_text = format_analysis_payment_admin_text(request, user)
        user_text = format_analysis_payment_text(request)
        await session.commit()

    if callback.message:
        await callback.message.edit_text(user_text, reply_markup=analysis_payment_keyboard(request.id))
    await notify_admins(callback.bot, settings.admin_ids, admin_text, reply_markup=analysis_issue_keyboard(request.id))
    await callback.answer("Оплата подтверждена", show_alert=True)


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
}))
async def placeholder_handler(message: Message) -> None:
    await message.answer("Раздел подготовлен в меню и будет подключён на следующих этапах.")
