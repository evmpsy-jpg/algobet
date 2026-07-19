from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from aiogram import F, Router
from aiogram.enums import ContentType
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import desc, func, select

from app.settings import get_settings
from app.database.models import ImportBatch, Match, ScheduledSignal, User
from app.database.session import SessionFactory
from app.keyboards.common import admin_menu
from app.services.excel_parser import ParsedMatch
from app.services.import_service import import_tournaments
from app.services.signal_rules import analyze_match, build_signal_message
from app.services.rules_config import get_signal_rules, reload_signal_rules

router = Router(name="admin")
PAGE_SIZE = 8

STATUS_LABELS = {
    "scheduled": "🟢 Запланированные",
    "ready": "🟡 Готовые к отправке",
    "sent": "📤 Отправленные",
    "cancelled": "❌ Отменённые",
}


class UploadStates(StatesGroup):
    waiting_for_file = State()


def is_admin_user(user_id: int | None) -> bool:
    return bool(user_id and user_id in get_settings().admin_ids)


def is_admin(message: Message) -> bool:
    return is_admin_user(message.from_user.id if message.from_user else None)


def signals_dashboard_keyboard(counts: dict[str, int]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"🟢 Запланированные ({counts.get('scheduled', 0)})", callback_data="sig:list:scheduled:0")],
        [InlineKeyboardButton(text=f"🟡 Готовые ({counts.get('ready', 0)})", callback_data="sig:list:ready:0")],
        [InlineKeyboardButton(text=f"📤 Отправленные ({counts.get('sent', 0)})", callback_data="sig:list:sent:0")],
        [InlineKeyboardButton(text=f"❌ Отменённые ({counts.get('cancelled', 0)})", callback_data="sig:list:cancelled:0")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="sig:dashboard")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def signal_list_keyboard(items: list[tuple[ScheduledSignal, Match]], status: str, page: int, total: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for signal, match in items:
        side = signal.signal_payload.get("side") if signal.signal_payload else None
        side_text = f"П{side}" if side in (1, 2) else "—"
        rows.append([
            InlineKeyboardButton(
                text=f"{match.match_time} · {match.player_1} — {match.player_2} · {side_text}",
                callback_data=f"sig:view:{signal.id}:{status}:{page}",
            )
        ])
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"sig:list:{status}:{page-1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"sig:list:{status}:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ К разделу сигналов", callback_data="sig:dashboard")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def signal_detail_keyboard(signal_id: int, status: str, page: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if status in {"scheduled", "ready"}:
        rows.append([InlineKeyboardButton(text="📨 Отправить себе сейчас", callback_data=f"sig:preview:{signal_id}:{status}:{page}")])
        rows.append([
            InlineKeyboardButton(text="❌ Отменить", callback_data=f"sig:cancel:{signal_id}:{status}:{page}"),
            InlineKeyboardButton(text="🔄 Пересчитать", callback_data=f"sig:recalc:{signal_id}:{status}:{page}"),
        ])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"sig:list:{status}:{page}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def get_signal_counts() -> dict[str, int]:
    async with SessionFactory() as session:
        rows = (await session.execute(
            select(ScheduledSignal.status, func.count(ScheduledSignal.id)).group_by(ScheduledSignal.status)
        )).all()
    return dict(rows)


async def show_dashboard(target: Message | CallbackQuery) -> None:
    counts = await get_signal_counts()
    total = sum(counts.values())
    text = (
        "📊 <b>Сигналы</b>\n\n"
        f"Всего записей: {total}\n"
        f"🟢 Запланировано: {counts.get('scheduled', 0)}\n"
        f"🟡 Готово к отправке: {counts.get('ready', 0)}\n"
        f"📤 Отправлено: {counts.get('sent', 0)}\n"
        f"❌ Отменено: {counts.get('cancelled', 0)}"
    )
    markup = signals_dashboard_keyboard(counts)
    if isinstance(target, CallbackQuery):
        if target.message:
            await target.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup, parse_mode="HTML")


@router.message(F.text == "⚙️ Админ-панель")
async def admin_panel(message: Message) -> None:
    if not is_admin(message):
        await message.answer("Доступ запрещён.")
        return
    await message.answer("⚙️ Админ-панель", reply_markup=admin_menu())


@router.message(F.text.in_({"📥 Импорт Excel", "📥 Загрузить таблицу"}))
async def request_upload(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        await message.answer("Доступ запрещён.")
        return
    await state.set_state(UploadStates.waiting_for_file)
    await message.answer("Отправьте Excel-файл .xlsx с листом «Турниры».")


@router.message(UploadStates.waiting_for_file, F.content_type == ContentType.DOCUMENT)
async def receive_upload(message: Message, state: FSMContext) -> None:
    if not is_admin(message) or message.document is None or message.from_user is None:
        return
    file_name = message.document.file_name or "tournaments.xlsx"
    if not file_name.lower().endswith(".xlsx"):
        await message.answer("Нужен файл в формате .xlsx.")
        return
    settings = get_settings()
    max_bytes = settings.max_upload_mb * 1024 * 1024
    if message.document.file_size and message.document.file_size > max_bytes:
        await message.answer(f"Файл больше допустимых {settings.max_upload_mb} МБ.")
        return
    safe_name = Path(file_name).name
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    destination = settings.uploads_dir / f"{timestamp}_{safe_name}"
    await message.bot.download(message.document, destination=destination)
    await message.answer("Файл получен. Анализирую и обновляю данные…")
    try:
        async with SessionFactory() as session:
            summary = await import_tournaments(session, destination, safe_name, message.from_user.id)
    except Exception as exc:
        await message.answer(f"Ошибка обработки файла: {exc}")
        return
    finally:
        await state.clear()
    warning_text = ""
    if summary.warnings:
        preview = "\n".join(f"• {item}" for item in summary.warnings[:5])
        warning_text = f"\n\nПредупреждения ({len(summary.warnings)}):\n{preview}"
    await message.answer(
        "✅ Загрузка завершена\n\n"
        f"Строк в листе: {summary.total_rows}\n"
        f"Распознано матчей: {summary.parsed_matches}\n"
        f"Новых матчей: {summary.inserted_matches}\n"
        f"Обновлено матчей: {summary.updated_matches}\n"
        f"Отсутствуют в свежей таблице: {summary.missing_matches}\n"
        f"Запланировано сигналов: {summary.scheduled_signals}\n"
        f"Отменено сигналов: {summary.cancelled_signals}{warning_text}",
        reply_markup=admin_menu(),
    )


@router.message(F.text == "📊 Сигналы")
async def signals_menu(message: Message) -> None:
    if is_admin(message):
        await show_dashboard(message)


@router.callback_query(F.data == "sig:dashboard")
async def signals_dashboard_callback(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id):
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    await show_dashboard(callback)


@router.callback_query(F.data.startswith("sig:list:"))
async def signals_list_callback(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id) or not callback.data or not callback.message:
        return
    _, _, status, page_raw = callback.data.split(":")
    page = max(0, int(page_raw))
    async with SessionFactory() as session:
        total = int(await session.scalar(select(func.count(ScheduledSignal.id)).where(ScheduledSignal.status == status)) or 0)
        rows = (await session.execute(
            select(ScheduledSignal, Match)
            .join(Match, Match.id == ScheduledSignal.match_id)
            .where(ScheduledSignal.status == status)
            .order_by(ScheduledSignal.send_at.asc())
            .offset(page * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )).all()
    label = STATUS_LABELS.get(status, status)
    if not rows:
        text = f"{label}\n\nСписок пуст."
    else:
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        text = f"{label}\n\nСтраница {page + 1} из {pages}. Выберите матч:"
    await callback.message.edit_text(text, reply_markup=signal_list_keyboard(rows, status, page, total))
    await callback.answer()


@router.callback_query(F.data.startswith("sig:view:"))
async def signal_view_callback(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id) or not callback.data or not callback.message:
        return
    _, _, signal_id_raw, status, page_raw = callback.data.split(":")
    signal_id, page = int(signal_id_raw), int(page_raw)
    async with SessionFactory() as session:
        row = (await session.execute(
            select(ScheduledSignal, Match).join(Match, Match.id == ScheduledSignal.match_id)
            .where(ScheduledSignal.id == signal_id)
        )).first()
    if row is None:
        await callback.answer("Сигнал не найден", show_alert=True)
        return
    signal, match = row
    # Администратор видит ровно тот же текст, который получит пользователь.
    await callback.message.edit_text(
        signal.message_text or "Текст сигнала отсутствует",
        reply_markup=signal_detail_keyboard(signal.id, status, page),
        disable_web_page_preview=True,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("sig:preview:"))
async def signal_preview_callback(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id) or not callback.data or not callback.message:
        return
    _, _, signal_id_raw, _, _ = callback.data.split(":")
    async with SessionFactory() as session:
        signal = await session.get(ScheduledSignal, int(signal_id_raw))
    if signal is None or not signal.message_text:
        await callback.answer("Текст сигнала не найден", show_alert=True)
        return
    await callback.message.answer("🧪 Тестовая отправка администратору:\n\n" + signal.message_text)
    await callback.answer("Отправлено вам. Статус сигнала не изменён.")


@router.callback_query(F.data.startswith("sig:cancel:"))
async def signal_cancel_callback(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id) or not callback.data or not callback.message:
        return
    _, _, signal_id_raw, status, page_raw = callback.data.split(":")
    async with SessionFactory() as session:
        signal = await session.get(ScheduledSignal, int(signal_id_raw))
        if signal is None:
            await callback.answer("Сигнал не найден", show_alert=True)
            return
        signal.status = "cancelled"
        signal.cancel_reason = f"Отменён администратором {datetime.now():%d.%m.%Y %H:%M}"
        signal.recalculated_at = datetime.utcnow()
        await session.commit()
    await callback.answer("Сигнал отменён")
    callback.data = f"sig:list:{status}:{page_raw}"
    await signals_list_callback(callback)


@router.callback_query(F.data.startswith("sig:recalc:"))
async def signal_recalc_callback(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id) or not callback.data or not callback.message:
        return
    _, _, signal_id_raw, status, page_raw = callback.data.split(":")
    settings = get_settings()
    async with SessionFactory() as session:
        row = (await session.execute(
            select(ScheduledSignal, Match).join(Match, Match.id == ScheduledSignal.match_id)
            .where(ScheduledSignal.id == int(signal_id_raw))
        )).first()
        if row is None:
            await callback.answer("Сигнал не найден", show_alert=True)
            return
        signal, match = row
        raw = match.raw_data or {}
        parsed = ParsedMatch(
            external_match_id=match.external_match_id,
            external_tournament_id=match.external_tournament_id,
            source_url=match.source_url,
            tournament_date=match.tournament_date,
            tournament_name=str(raw.get("_tournament_name") or "Турнир"),
            match_time=match.match_time,
            match_start_at=match.match_start_at,
            player_1=match.player_1,
            player_2=match.player_2,
            player_1_rating=match.player_1_rating,
            player_2_rating=match.player_2_rating,
            score=match.score,
            raw_data=raw,
        )
        decision = analyze_match(parsed)
        if decision.suitable:
            signal.status = "scheduled"
            lead_minutes = int(get_signal_rules()["signal"].get("lead_minutes", settings.signal_lead_minutes))
            signal.send_at = match.match_start_at - timedelta(minutes=lead_minutes)
            signal.signal_type = decision.signal_type
            signal.signal_payload = decision.payload or {}
            signal.message_text = build_signal_message(parsed, decision)
            signal.cancel_reason = None
            result_text = "Сигнал пересчитан и запланирован"
        else:
            signal.status = "cancelled"
            signal.cancel_reason = decision.reason or "Матч не соответствует условиям"
            result_text = "После пересчёта сигнал отменён"
        signal.recalculated_at = datetime.utcnow()
        await session.commit()
    await callback.answer(result_text, show_alert=True)
    callback.data = f"sig:list:{status}:{page_raw}"
    await signals_list_callback(callback)


@router.message(F.text == "📋 Последняя загрузка")
async def latest_import(message: Message) -> None:
    if not is_admin(message):
        return
    async with SessionFactory() as session:
        batch = await session.scalar(select(ImportBatch).order_by(desc(ImportBatch.id)).limit(1))
    if batch is None:
        await message.answer("Загрузок ещё не было.")
        return
    await message.answer(
        f"Последняя загрузка: {batch.file_name}\nСтатус: {batch.status}\nМатчей: {batch.parsed_matches}\n"
        f"Новых: {batch.inserted_matches}\nОбновлено: {batch.updated_matches}\nДата: {batch.created_at:%d.%m.%Y %H:%M:%S}"
    )


@router.message(F.text == "📤 История отправок")
async def sent_history(message: Message) -> None:
    if not is_admin(message):
        return
    async with SessionFactory() as session:
        sent = int(await session.scalar(select(func.count(ScheduledSignal.id)).where(ScheduledSignal.status == "sent")) or 0)
        ready = int(await session.scalar(select(func.count(ScheduledSignal.id)).where(ScheduledSignal.status == "ready")) or 0)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📤 Отправленные ({sent})", callback_data="sig:list:sent:0")],
        [InlineKeyboardButton(text=f"🟡 Готовые ({ready})", callback_data="sig:list:ready:0")],
    ])
    await message.answer("📤 История выдачи сигналов", reply_markup=markup)


@router.message(F.text == "📈 Статистика")
async def admin_statistics(message: Message) -> None:
    if not is_admin(message):
        return
    today = datetime.now().date()
    start = datetime.combine(today, datetime.min.time())
    end = start + timedelta(days=1)
    async with SessionFactory() as session:
        imports = int(await session.scalar(select(func.count(ImportBatch.id)).where(ImportBatch.created_at >= start, ImportBatch.created_at < end)) or 0)
        users = int(await session.scalar(select(func.count(User.id)).where(User.is_active.is_(True))) or 0)
        counts = dict((await session.execute(select(ScheduledSignal.status, func.count(ScheduledSignal.id)).group_by(ScheduledSignal.status))).all())
        next_signal = (await session.execute(
            select(ScheduledSignal, Match).join(Match, Match.id == ScheduledSignal.match_id)
            .where(ScheduledSignal.status == "scheduled")
            .order_by(ScheduledSignal.send_at.asc()).limit(1)
        )).first()
    next_text = "нет"
    if next_signal:
        signal, match = next_signal
        next_text = f"{signal.send_at:%d.%m %H:%M} · {match.player_1} — {match.player_2}"
    await message.answer(
        "📈 Статистика\n\n"
        f"Сегодня импортов: {imports}\nАктивных пользователей: {users}\n"
        f"Запланировано: {counts.get('scheduled', 0)}\nГотово: {counts.get('ready', 0)}\n"
        f"Отправлено: {counts.get('sent', 0)}\nОтменено: {counts.get('cancelled', 0)}\n\n"
        f"Следующий сигнал: {next_text}"
    )


@router.message(F.text == "👥 Пользователи")
async def users_info(message: Message) -> None:
    if not is_admin(message):
        return
    async with SessionFactory() as session:
        total = int(await session.scalar(select(func.count(User.id))) or 0)
        active = int(await session.scalar(select(func.count(User.id)).where(User.is_active.is_(True))) or 0)
    await message.answer(f"👥 Пользователи\n\nВсего: {total}\nАктивных: {active}\n\nУправление подписками добавим на следующем этапе.")


@router.message(F.text == "⚙️ Настройки")
async def settings_info(message: Message) -> None:
    if not is_admin(message):
        return
    settings = get_settings()
    rules = reload_signal_rules()
    signal = rules["signal"]
    high = rules["high_confidence"]
    await message.answer(
        "⚙️ Настройки\n\n"
        f"Часовой пояс: {settings.timezone}\n"
        f"Отправка до матча: {signal['lead_minutes']} минут\n"
        f"Минимум H2H (CP): {signal['min_h2h_games']}\n"
        f"Минимальная форма Q/X: {signal['min_favorite_form']}\n"
        f"Минимум BG для П1: {signal['min_bg_p1']}\n"
        f"Минимум BF для П2: {signal['min_bf_p2']}\n"
        f"ЖБ-сигнал от: {high['min_probability']}%\n"
        f"Проверка очереди: каждые {settings.scheduler_interval_seconds} секунд\n"
        f"Максимальный Excel: {settings.max_upload_mb} МБ\n\n"
        "Значения читаются из signal_rules.yaml. После изменения файла повторно откройте этот раздел и загрузите Excel заново."
    )
