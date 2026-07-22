from __future__ import annotations

from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

from aiogram import F, Router
from aiogram.enums import ContentType
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import desc, func, or_, select

from app.settings import get_settings
from app.database.models import ImportBatch, Match, ScheduledSignal, SignalDecisionLog, SignalDelivery, SignalResult, User, UserAccess
from app.database.session import SessionFactory
from app.keyboards.common import admin_menu
from app.services.access import disable_access, grant_paid_access, grant_trial_access
from app.services.decision_log import record_decision_log
from app.services.excel_parser import ParsedMatch
from app.services.import_service import import_tournaments
from app.services.signal_rules import analyze_match, build_signal_message
from app.services.rules_config import get_signal_rules, reload_signal_rules
from app.services.signal_sender import process_signal_now
from app.services.signal_results import LEVEL_ORDER, auto_update_signal_results, format_winrate, result_full_label, result_label, result_short_label, result_source_label, set_signal_result, summarize_results

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




def _local_dt(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(ZoneInfo(get_settings().timezone))


def _fmt_dt(value: datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    local = _local_dt(value)
    return local.strftime(fmt) if local else "—"

def _format_level_result_line(level: str, counter) -> str:
    return (
        f"{level}: ✅ {counter.won} / ❌ {counter.lost} / "
        f"↩️ {counter.void} / ❔ {counter.unknown} · WR {format_winrate(counter.winrate)}"
    )


def signals_dashboard_keyboard(counts: dict[str, int]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"🟢 Запланированные ({counts.get('scheduled', 0)})", callback_data="sig:list:scheduled:0")],
        [InlineKeyboardButton(text=f"🟡 Готовые ({counts.get('ready', 0)})", callback_data="sig:list:ready:0")],
        [InlineKeyboardButton(text=f"📤 Отправленные ({counts.get('sent', 0)})", callback_data="sig:list:sent:0")],
        [InlineKeyboardButton(text=f"❌ Отменённые ({counts.get('cancelled', 0)})", callback_data="sig:list:cancelled:0")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="sig:dashboard")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def signal_list_keyboard(
    items: list[tuple[ScheduledSignal, Match, SignalResult | None]],
    status: str,
    page: int,
    total: int,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for signal, match, result in items:
        side = signal.signal_payload.get("side") if signal.signal_payload else None
        side_text = f"П{side}" if side in (1, 2) else "—"
        result_text = result_short_label(result.status if result else None)
        rows.append([
            InlineKeyboardButton(
                text=f"{match.match_time} · {match.player_1} — {match.player_2} · {side_text} · {result_text}",
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

RESULT_FILTER_LABELS = {
    "unrated": "❔ Без результата",
    "won": "✅ Зашли",
    "lost": "❌ Не зашли",
    "void": "↩️ Возврат",
}


def result_filter_keyboard(
    items: list[tuple[ScheduledSignal, Match, SignalResult | None]],
    result_filter: str,
    page: int,
    total: int,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for signal, match, result in items:
        side = signal.signal_payload.get("side") if signal.signal_payload else None
        side_text = f"П{side}" if side in (1, 2) else "—"
        result_text = result_short_label(result.status if result else None)
        rows.append([
            InlineKeyboardButton(
                text=f"{match.match_time} · {match.player_1} — {match.player_2} · {side_text} · {result_text}",
                callback_data=f"sig:view:{signal.id}:sent:0",
            )
        ])
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"sig:rlist:{result_filter}:{page-1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"sig:rlist:{result_filter}:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ К истории", callback_data="sig:history")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def signal_detail_keyboard(signal_id: int, status: str, page: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    rows.append([InlineKeyboardButton(text="🧾 Трассировка решения", callback_data=f"sig:trace:{signal_id}:{status}:{page}")])
    rows.append([InlineKeyboardButton(text="📬 Доставки", callback_data=f"sig:deliveries:{signal_id}:{status}:{page}")])
    rows.append([
        InlineKeyboardButton(text="✅ Зашёл", callback_data=f"sig:result:{signal_id}:won:{status}:{page}"),
        InlineKeyboardButton(text="❌ Не зашёл", callback_data=f"sig:result:{signal_id}:lost:{status}:{page}"),
    ])
    rows.append([
        InlineKeyboardButton(text="↩️ Возврат", callback_data=f"sig:result:{signal_id}:void:{status}:{page}"),
        InlineKeyboardButton(text="❔ Неизвестно", callback_data=f"sig:result:{signal_id}:unknown:{status}:{page}"),
    ])
    if status in {"scheduled", "ready"}:
        rows.append([InlineKeyboardButton(text="📨 Отправить себе сейчас", callback_data=f"sig:preview:{signal_id}:{status}:{page}")])
        rows.append([InlineKeyboardButton(text="🚀 Отправить пользователям сейчас", callback_data=f"sig:sendnow:{signal_id}:{status}:{page}")])
        rows.append([
            InlineKeyboardButton(text="❌ Отменить", callback_data=f"sig:cancel:{signal_id}:{status}:{page}"),
            InlineKeyboardButton(text="🔄 Пересчитать", callback_data=f"sig:recalc:{signal_id}:{status}:{page}"),
        ])
    elif status == "sent":
        rows.append([InlineKeyboardButton(text="🔁 Повторить failed/missing", callback_data=f"sig:sendnow:{signal_id}:{status}:{page}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"sig:list:{status}:{page}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def format_decision_log(log: SignalDecisionLog, match: Match) -> str:
    status = "подходит" if log.suitable else "отклонён"
    side = f"П{log.side}" if log.side in (1, 2) else "—"
    probability = f"{log.probability:g}%" if log.probability is not None else "—"
    lines = [
        "🧾 Трассировка решения",
        "",
        f"Матч: {match.player_1} — {match.player_2}",
        f"Время: {match.tournament_date} {match.match_time}",
        f"Версия: {log.algorithm_version}",
        f"Источник: {log.source}",
        f"Итог: {status}",
        f"Сторона: {side}",
        f"Вероятность: {probability}",
        f"Уровень: {log.level or '—'}",
    ]
    if log.reason:
        lines.extend(["", f"Причина: {log.reason}"])
    lines.append("")
    lines.append("Проверки:")
    for item in (log.decision_trace or [])[:20]:
        mark = "✅" if item.get("passed") else "❌"
        label = item.get("label") or item.get("code") or "Правило"
        actual = item.get("actual")
        expected = item.get("expected")
        side_text = f" П{item.get('side')}" if item.get("side") in (1, 2) else ""
        lines.append(f"{mark}{side_text} {label}: {actual} / нужно {expected}")
    return "\n".join(lines)[:3900]



def _delivery_status_label(status: str) -> str:
    labels = {
        "pending": "ожидает",
        "sent": "отправлено",
        "failed": "ошибка",
    }
    return labels.get(status, status)


def format_signal_deliveries(
    signal: ScheduledSignal,
    match: Match,
    rows: list[tuple[SignalDelivery, User | None]],
) -> str:
    counts: dict[str, int] = {}
    for delivery, _ in rows:
        counts[delivery.status] = counts.get(delivery.status, 0) + 1
    lines = [
        "📬 Доставки сигнала",
        "",
        f"Матч: {match.player_1} — {match.player_2}",
        f"Время сигнала: {_fmt_dt(signal.send_at)}",
        f"Статус сигнала: {signal.status}",
        "",
        f"Всего доставок: {len(rows)}",
        f"Отправлено: {counts.get('sent', 0)}",
        f"Ошибок: {counts.get('failed', 0)}",
        f"Ожидает: {counts.get('pending', 0)}",
    ]
    if not rows:
        lines.extend(["", "Доставок по этому сигналу пока нет."])
        return "\n".join(lines)
    lines.append("")
    lines.append("Последние доставки:")
    for delivery, user in rows[:25]:
        name = _user_name(user) if user else str(delivery.telegram_id)
        sent_at = _fmt_dt(delivery.sent_at, "%d.%m %H:%M")
        lines.append(
            f"• {name} · {delivery.telegram_id} · "
            f"{_delivery_status_label(delivery.status)} · {sent_at}"
        )
        if delivery.error_text:
            error = delivery.error_text.replace("\n", " ")[:240]
            lines.append(f"  Ошибка: {error}")
    return "\n".join(lines)[:3900]
def format_sent_history_summary(
    *,
    sent: int,
    ready: int,
    delivered: int,
    failed: int,
    recent_rows: list[tuple[ScheduledSignal, Match, SignalResult | None]],
    delivery_counts: dict[int, dict[str, int]],
) -> str:
    lines = [
        "📤 История выдачи сигналов",
        "",
        f"Отправленных сигналов: {sent}",
        f"Готовых к отправке: {ready}",
        f"Успешных доставок пользователям: {delivered}",
        f"Ошибок доставки: {failed}",
        "",
        "Последние отправленные:",
    ]
    if not recent_rows:
        lines.append("пока нет отправленных сигналов")
        return "\n".join(lines)

    for signal, match, result in recent_rows[:10]:
        payload = signal.signal_payload or {}
        side = payload.get("side")
        side_text = f"П{side}" if side in (1, 2) else "—"
        level = payload.get("level") or "—"
        sent_at = _fmt_dt(signal.sent_at or signal.send_at, "%d.%m %H:%M")
        counts = delivery_counts.get(signal.id, {})
        lines.extend([
            f"• {sent_at} · {result_short_label(result.status if result else None)} {result_source_label(result.source if result else None)} · {level} · {side_text}",
            f"  {match.player_1} — {match.player_2}",
            f"  доставки: ✅ {counts.get('sent', 0)} / ❌ {counts.get('failed', 0)} / ⏳ {counts.get('pending', 0)}",
        ])
    return "\n".join(lines)[:3900]

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
            select(ScheduledSignal, Match, SignalResult)
            .join(Match, Match.id == ScheduledSignal.match_id)
            .outerjoin(SignalResult, SignalResult.signal_id == ScheduledSignal.id)
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

@router.callback_query(F.data.startswith("sig:rlist:"))
async def signal_result_list_callback(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id) or not callback.data or not callback.message:
        return
    _, _, result_filter, page_raw = callback.data.split(":")
    page = max(0, int(page_raw))
    if result_filter not in RESULT_FILTER_LABELS:
        await callback.answer("Неизвестный фильтр", show_alert=True)
        return

    if result_filter == "unrated":
        condition = or_(SignalResult.id.is_(None), SignalResult.status == "unknown")
    else:
        condition = SignalResult.status == result_filter

    async with SessionFactory() as session:
        total = int(await session.scalar(
            select(func.count(ScheduledSignal.id))
            .outerjoin(SignalResult, SignalResult.signal_id == ScheduledSignal.id)
            .where(ScheduledSignal.status == "sent")
            .where(condition)
        ) or 0)
        rows = list((await session.execute(
            select(ScheduledSignal, Match, SignalResult)
            .join(Match, Match.id == ScheduledSignal.match_id)
            .outerjoin(SignalResult, SignalResult.signal_id == ScheduledSignal.id)
            .where(ScheduledSignal.status == "sent")
            .where(condition)
            .order_by(desc(ScheduledSignal.sent_at), desc(ScheduledSignal.id))
            .offset(page * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )).all())

    label = RESULT_FILTER_LABELS[result_filter]
    if rows:
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        text = f"{label}\n\nСтраница {page + 1} из {pages}. Выберите сигнал:"
    else:
        text = f"{label}\n\nСписок пуст."
    await callback.message.edit_text(text, reply_markup=result_filter_keyboard(rows, result_filter, page, total))
    await callback.answer()

@router.callback_query(F.data.startswith("sig:view:"))
async def signal_view_callback(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id) or not callback.data or not callback.message:
        return
    _, _, signal_id_raw, status, page_raw = callback.data.split(":")
    signal_id, page = int(signal_id_raw), int(page_raw)
    async with SessionFactory() as session:
        row = (await session.execute(
            select(ScheduledSignal, Match, SignalResult)
            .join(Match, Match.id == ScheduledSignal.match_id)
            .outerjoin(SignalResult, SignalResult.signal_id == ScheduledSignal.id)
            .where(ScheduledSignal.id == signal_id)
        )).first()
    if row is None:
        await callback.answer("Сигнал не найден", show_alert=True)
        return
    signal, match, result = row
    text = f"Результат: {result_full_label(result)}\n\n" + (signal.message_text or "Текст сигнала отсутствует")
    await callback.message.edit_text(
        text,
        reply_markup=signal_detail_keyboard(signal.id, status, page),
        disable_web_page_preview=True,
    )
    await callback.answer()

@router.callback_query(F.data.startswith("sig:result:"))
async def signal_result_callback(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id) or not callback.data or not callback.message:
        return
    _, _, signal_id_raw, result_status, status, page_raw = callback.data.split(":")
    signal_id, page = int(signal_id_raw), int(page_raw)
    async with SessionFactory() as session:
        signal = await session.get(ScheduledSignal, signal_id)
        if signal is None:
            await callback.answer("Сигнал не найден", show_alert=True)
            return
        result = await set_signal_result(session, signal, result_status, callback.from_user.id)
    await callback.answer(f"Результат: {result_label(result.status)}")
    callback.data = f"sig:view:{signal_id}:{status}:{page}"
    await signal_view_callback(callback)

@router.callback_query(F.data.startswith("sig:trace:"))
async def signal_trace_callback(callback: CallbackQuery) -> None:
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
        log = await session.scalar(
            select(SignalDecisionLog)
            .where(SignalDecisionLog.match_id == match.id)
            .order_by(desc(SignalDecisionLog.id))
            .limit(1)
        )
    if log is None:
        await callback.answer("Трассировка ещё не записана", show_alert=True)
        return
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ К сигналу", callback_data=f"sig:view:{signal_id}:{status}:{page}")],
    ])
    await callback.message.edit_text(format_decision_log(log, match), reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.startswith("sig:deliveries:"))
async def signal_deliveries_callback(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id) or not callback.data or not callback.message:
        return
    _, _, signal_id_raw, status, page_raw = callback.data.split(":")
    signal_id, page = int(signal_id_raw), int(page_raw)
    async with SessionFactory() as session:
        row = (await session.execute(
            select(ScheduledSignal, Match)
            .join(Match, Match.id == ScheduledSignal.match_id)
            .where(ScheduledSignal.id == signal_id)
        )).first()
        if row is None:
            await callback.answer("Сигнал не найден", show_alert=True)
            return
        signal, match = row
        deliveries = list((await session.execute(
            select(SignalDelivery, User)
            .outerjoin(User, User.id == SignalDelivery.user_id)
            .where(SignalDelivery.signal_id == signal.id)
            .order_by(desc(SignalDelivery.id))
            .limit(25)
        )).all())
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ К сигналу", callback_data=f"sig:view:{signal_id}:{status}:{page}")],
    ])
    await callback.message.edit_text(
        format_signal_deliveries(signal, match, deliveries),
        reply_markup=markup,
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



@router.callback_query(F.data.startswith("sig:sendnow:"))
async def signal_send_now_callback(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id) or not callback.data or not callback.message:
        return
    _, _, signal_id_raw, status, page_raw = callback.data.split(":")
    signal_id, page = int(signal_id_raw), int(page_raw)
    async with SessionFactory() as session:
        signal = await session.get(ScheduledSignal, signal_id)
        if signal is None:
            await callback.answer("Сигнал не найден", show_alert=True)
            return
        if signal.status == "cancelled":
            await callback.answer("Отменённый сигнал нельзя отправить", show_alert=True)
            return
        summary = await process_signal_now(callback.message.bot, session, signal_id)
    await callback.answer(
        "Доставка завершена: "
        f"отправлено {summary.sent_deliveries}, "
        f"ошибок {summary.failed_deliveries}, "
        f"пропущено {summary.skipped_users}",
        show_alert=True,
    )
    callback.data = f"sig:deliveries:{signal_id}:{status}:{page}"
    await signal_deliveries_callback(callback)
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
        await record_decision_log(
            session,
            match_id=match.id,
            decision=decision,
            import_batch_id=match.current_import_id,
            source="manual_recalc",
        )
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
        f"Новых: {batch.inserted_matches}\nОбновлено: {batch.updated_matches}\nДата: {_fmt_dt(batch.created_at, '%d.%m.%Y %H:%M:%S')}"
    )


async def show_sent_history(target: Message | CallbackQuery) -> None:
    async with SessionFactory() as session:
        sent = int(await session.scalar(select(func.count(ScheduledSignal.id)).where(ScheduledSignal.status == "sent")) or 0)
        ready = int(await session.scalar(select(func.count(ScheduledSignal.id)).where(ScheduledSignal.status == "ready")) or 0)
        delivered = int(await session.scalar(select(func.count(SignalDelivery.id)).where(SignalDelivery.status == "sent")) or 0)
        failed = int(await session.scalar(select(func.count(SignalDelivery.id)).where(SignalDelivery.status == "failed")) or 0)
        recent_rows = list((await session.execute(
            select(ScheduledSignal, Match, SignalResult)
            .join(Match, Match.id == ScheduledSignal.match_id)
            .outerjoin(SignalResult, SignalResult.signal_id == ScheduledSignal.id)
            .where(ScheduledSignal.status == "sent")
            .order_by(desc(ScheduledSignal.sent_at), desc(ScheduledSignal.id))
            .limit(10)
        )).all())
        signal_ids = [signal.id for signal, _, _ in recent_rows]
        delivery_counts: dict[int, dict[str, int]] = {}
        if signal_ids:
            delivery_rows = (await session.execute(
                select(SignalDelivery.signal_id, SignalDelivery.status, func.count(SignalDelivery.id))
                .where(SignalDelivery.signal_id.in_(signal_ids))
                .group_by(SignalDelivery.signal_id, SignalDelivery.status)
            )).all()
            for signal_id, delivery_status, count in delivery_rows:
                delivery_counts.setdefault(signal_id, {})[delivery_status] = int(count)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📤 Отправленные ({sent})", callback_data="sig:list:sent:0")],
        [InlineKeyboardButton(text="🔄 Автооценить по счету", callback_data="sig:autoresults")],
        [
            InlineKeyboardButton(text="❔ Без результата", callback_data="sig:rlist:unrated:0"),
            InlineKeyboardButton(text="✅ Зашли", callback_data="sig:rlist:won:0"),
        ],
        [
            InlineKeyboardButton(text="❌ Не зашли", callback_data="sig:rlist:lost:0"),
            InlineKeyboardButton(text="↩️ Возврат", callback_data="sig:rlist:void:0"),
        ],
        [InlineKeyboardButton(text=f"🟡 Готовые ({ready})", callback_data="sig:list:ready:0")],
    ])
    text = format_sent_history_summary(
        sent=sent,
        ready=ready,
        delivered=delivered,
        failed=failed,
        recent_rows=recent_rows,
        delivery_counts=delivery_counts,
    )
    if isinstance(target, CallbackQuery):
        if target.message:
            await target.message.edit_text(text, reply_markup=markup)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


@router.message(F.text == "📤 История отправок")
async def sent_history(message: Message) -> None:
    if not is_admin(message):
        return
    await show_sent_history(message)


@router.callback_query(F.data == "sig:autoresults")
async def signal_auto_results_callback(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id):
        details = ""
    if summary.updated_items:
        preview = "\n".join(summary.updated_items[:5])
        more = f"\n… ещё {len(summary.updated_items) - 5}" if len(summary.updated_items) > 5 else ""
        details = f"\n\nОбновлено:\n{preview}{more}"
    await callback.answer(
        "Автооценка завершена: "
        f"обновлено {summary.updated}, "
        f"без изменений {summary.unchanged}, "
        f"ручных пропущено {summary.skipped_manual}, "
        f"без счета {summary.no_score}"
        f"{details}",
        show_alert=True,
    )
    await show_sent_history(callback)

@router.callback_query(F.data == "sig:history")
async def sent_history_callback(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id):
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    await show_sent_history(callback)

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
        delivery_counts = dict((await session.execute(select(SignalDelivery.status, func.count(SignalDelivery.id)).group_by(SignalDelivery.status))).all())
        result_rows = list((await session.execute(
            select(ScheduledSignal.signal_payload, SignalResult.status)
            .join(SignalResult, SignalResult.signal_id == ScheduledSignal.id)
        )).all())
        next_signal = (await session.execute(
            select(ScheduledSignal, Match).join(Match, Match.id == ScheduledSignal.match_id)
            .where(ScheduledSignal.status == "scheduled")
            .order_by(ScheduledSignal.send_at.asc()).limit(1)
        )).first()
    next_text = "нет"
    if next_signal:
        signal, match = next_signal
        next_text = f"{_fmt_dt(signal.send_at, '%d.%m %H:%M')} · {match.player_1} — {match.player_2}"

    sent_total = counts.get("sent", 0)
    result_summary = summarize_results(result_rows, total_sent=sent_total)
    level_lines = []
    for level in LEVEL_ORDER:
        if level in result_summary.by_level:
            level_lines.append(_format_level_result_line(level, result_summary.by_level[level]))
    for level in sorted(set(result_summary.by_level) - set(LEVEL_ORDER)):
        level_lines.append(_format_level_result_line(level, result_summary.by_level[level]))
    level_text = "\n".join(level_lines) if level_lines else "пока нет зафиксированных результатов"

    await message.answer(
        "📈 Статистика\n\n"
        f"Сегодня импортов: {imports}\nАктивных пользователей: {users}\n"
        f"Запланировано: {counts.get('scheduled', 0)}\nГотово: {counts.get('ready', 0)}\n"
        f"Отправлено: {sent_total}\nОтменено: {counts.get('cancelled', 0)}\n"
        f"Доставлено пользователям: {delivery_counts.get('sent', 0)}\nОшибок доставки: {delivery_counts.get('failed', 0)}\n\n"
        "Результаты сигналов:\n"
        f"✅ Зашло: {result_summary.overall.won}\n"
        f"❌ Не зашло: {result_summary.overall.lost}\n"
        f"↩️ Возврат: {result_summary.overall.void}\n"
        f"❔ Неизвестно: {result_summary.overall.unknown}\n"
        f"Оценено: {result_summary.evaluated} из {result_summary.total_sent}\n"
        f"Без результата: {result_summary.unrated_sent}\n"
        f"Winrate: {format_winrate(result_summary.overall.winrate)}\n\n"
        f"По уровням:\n{level_text}\n\n"
        f"Следующий сигнал: {next_text}"
    )

def _user_name(user: User) -> str:
    parts = [item for item in [user.first_name, user.last_name] if item]
    if parts:
        return " ".join(parts)
    if user.username:
        return f"@{user.username}"
    return str(user.telegram_id)


def _access_label(access: UserAccess | None) -> str:
    if access is None:
        return "нет доступа"
    if access.status != "active":
        return "отключён"
    if access.access_type == "trial":
        return f"trial · осталось {access.free_signals_remaining}"
    if access.access_type == "paid":
        until = f" до {access.active_until:%d.%m.%Y}" if access.active_until else ""
        return f"paid{until}"
    return access.access_type


def users_list_keyboard(items: list[tuple[User, UserAccess | None]], page: int, total: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for user, access in items:
        rows.append([
            InlineKeyboardButton(
                text=f"{_user_name(user)} · {_access_label(access)}",
                callback_data=f"usr:view:{user.id}:{page}",
            )
        ])
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"usr:list:{page-1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"usr:list:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data=f"usr:list:{page}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def user_detail_keyboard(user_id: int, page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Выдать trial 3", callback_data=f"usr:trial:{user_id}:{page}")],
        [InlineKeyboardButton(text="💳 Выдать paid", callback_data=f"usr:paid:{user_id}:{page}")],
        [InlineKeyboardButton(text="⛔ Отключить доступ", callback_data=f"usr:disable:{user_id}:{page}")],
        [InlineKeyboardButton(text="⬅️ К пользователям", callback_data=f"usr:list:{page}")],
    ])


def format_user_detail(user: User, access: UserAccess | None, delivered: int, failed: int) -> str:
    username = f"@{user.username}" if user.username else "—"
    created = _fmt_dt(user.created_at)
    return (
        "👤 Пользователь\n\n"
        f"Имя: {_user_name(user)}\n"
        f"Telegram ID: {user.telegram_id}\n"
        f"Username: {username}\n"
        f"Активен: {'да' if user.is_active else 'нет'}\n"
        f"Создан: {created}\n\n"
        f"Доступ: {_access_label(access)}\n"
        f"Успешных доставок: {delivered}\n"
        f"Ошибок доставки: {failed}"
    )


async def show_users_list(target: Message | CallbackQuery, page: int = 0) -> None:
    page = max(0, page)
    async with SessionFactory() as session:
        total = int(await session.scalar(select(func.count(User.id))) or 0)
        rows = list((await session.execute(
            select(User, UserAccess)
            .outerjoin(UserAccess, UserAccess.user_id == User.id)
            .order_by(desc(User.id))
            .offset(page * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )).all())
        active = int(await session.scalar(select(func.count(User.id)).where(User.is_active.is_(True))) or 0)
        trial = int(await session.scalar(select(func.count(UserAccess.id)).where(UserAccess.status == "active", UserAccess.access_type == "trial")) or 0)
        paid = int(await session.scalar(select(func.count(UserAccess.id)).where(UserAccess.status == "active", UserAccess.access_type == "paid")) or 0)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    text = (
        "👥 Пользователи\n\n"
        f"Всего: {total}\n"
        f"Активных: {active}\n"
        f"Пробный доступ: {trial}\n"
        f"Платный доступ: {paid}\n\n"
        f"Страница {page + 1} из {pages}."
    )
    markup = users_list_keyboard(rows, page, total)
    if isinstance(target, CallbackQuery):
        if target.message:
            await target.message.edit_text(text, reply_markup=markup)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


async def show_user_detail(callback: CallbackQuery, user_id: int, page: int) -> None:
    if not callback.message:
        return
    async with SessionFactory() as session:
        row = (await session.execute(
            select(User, UserAccess)
            .outerjoin(UserAccess, UserAccess.user_id == User.id)
            .where(User.id == user_id)
        )).first()
        if row is None:
            await callback.answer("Пользователь не найден", show_alert=True)
            return
        user, access = row
        delivered = int(await session.scalar(select(func.count(SignalDelivery.id)).where(SignalDelivery.user_id == user.id, SignalDelivery.status == "sent")) or 0)
        failed = int(await session.scalar(select(func.count(SignalDelivery.id)).where(SignalDelivery.user_id == user.id, SignalDelivery.status == "failed")) or 0)
    await callback.message.edit_text(
        format_user_detail(user, access, delivered, failed),
        reply_markup=user_detail_keyboard(user_id, page),
    )
    await callback.answer()
@router.message(F.text == "👥 Пользователи")
async def users_info(message: Message) -> None:
    if not is_admin(message):
        return
    await show_users_list(message)


@router.callback_query(F.data.startswith("usr:list:"))
async def users_list_callback(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id) or not callback.data:
        return
    _, _, page_raw = callback.data.split(":")
    await show_users_list(callback, int(page_raw))


@router.callback_query(F.data.startswith("usr:view:"))
async def user_view_callback(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id) or not callback.data:
        return
    _, _, user_id_raw, page_raw = callback.data.split(":")
    await show_user_detail(callback, int(user_id_raw), int(page_raw))


@router.callback_query(F.data.startswith("usr:trial:"))
async def user_grant_trial_callback(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id) or not callback.data:
        return
    _, _, user_id_raw, page_raw = callback.data.split(":")
    user_id, page = int(user_id_raw), int(page_raw)
    async with SessionFactory() as session:
        user = await session.get(User, user_id)
        if user is None:
            await callback.answer("Пользователь не найден", show_alert=True)
            return
        await grant_trial_access(session, user)
        await session.commit()
    await callback.answer("Trial-доступ выдан", show_alert=True)
    await show_user_detail(callback, user_id, page)


@router.callback_query(F.data.startswith("usr:paid:"))
async def user_grant_paid_callback(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id) or not callback.data:
        return
    _, _, user_id_raw, page_raw = callback.data.split(":")
    user_id, page = int(user_id_raw), int(page_raw)
    async with SessionFactory() as session:
        user = await session.get(User, user_id)
        if user is None:
            await callback.answer("Пользователь не найден", show_alert=True)
            return
        await grant_paid_access(session, user)
        await session.commit()
    await callback.answer("Paid-доступ выдан", show_alert=True)
    await show_user_detail(callback, user_id, page)


@router.callback_query(F.data.startswith("usr:disable:"))
async def user_disable_access_callback(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id) or not callback.data:
        return
    _, _, user_id_raw, page_raw = callback.data.split(":")
    user_id, page = int(user_id_raw), int(page_raw)
    async with SessionFactory() as session:
        user = await session.get(User, user_id)
        if user is None:
            await callback.answer("Пользователь не найден", show_alert=True)
            return
        await disable_access(session, user)
        await session.commit()
    await callback.answer("Доступ отключён", show_alert=True)
    await show_user_detail(callback, user_id, page)

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
