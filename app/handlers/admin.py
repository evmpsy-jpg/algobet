from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

from aiogram import F, Router
from aiogram.enums import ContentType
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import desc, func, or_, select

from app.settings import get_settings
from app.database.models import ImportBatch, Match, MatchAnalysisRequest, ScheduledSignal, SignalDecisionLog, SignalDelivery, SignalResult, SubscriptionRequest, User, UserAccess
from app.database.session import SessionFactory
from app.keyboards.common import admin_menu
from app.services.access import disable_access, grant_paid_access, grant_subscription_access, grant_trial_access
from app.services.admin_notifications import format_import_error_admin_text, format_import_success_admin_text, notify_admins
from app.services.bot_settings import (
    ANALYSIS_PAYMENT_DETAILS_KEY,
    ANALYSIS_SPECIALIST_CONTACT_KEY,
    SUBSCRIPTION_PAYMENT_DETAILS_KEY,
    SUBSCRIPTION_SPECIALIST_CONTACT_KEY,
    get_analysis_payment_config,
    get_subscription_payment_config,
    set_bot_setting,
)
from app.services.dashboard import SignalListItem, collect_import_signal_schedule_warnings
from app.services.decision_log import record_decision_log
from app.services.excel_parser import ParsedMatch
from app.services.import_service import import_tournaments, to_utc_naive
from app.services.match_analysis import build_match_analysis_text, format_analysis_status_user_text
from app.services.signal_rules import analyze_match, build_signal_message
from app.services.rules_config import get_signal_rules, reload_signal_rules
from app.services.signal_sender import process_signal_now
from app.services.signal_results import auto_update_signal_results, format_winrate, result_full_label, result_label, result_short_label, result_source_label, set_signal_result, signal_group_title, signal_stats_eligible, summarize_results
from app.services.signal_tariffs import SIGNAL_TARIFF_ORDER, signal_tariff_title
from app.services.sqlite_backup import create_sqlite_backup, latest_sqlite_backup, sqlite_database_path
from app.services.subscriptions import SUBSCRIPTION_STATUS_LABELS, format_price, format_subscription_activation_user_text

router = Router(name="admin")



async def _refresh_signal_message_from_match(session, signal: ScheduledSignal, match: Match | None = None) -> None:
    if match is None:
        match = await session.get(Match, signal.match_id)
    if match is None or not match.raw_data:
        return
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
    if not decision.suitable:
        return
    signal.signal_type = decision.signal_type
    signal.signal_payload = decision.payload or {}
    signal.message_text = build_signal_message(parsed, decision)
async def safe_edit_text(message: Message, text: str, **kwargs: object) -> None:
    try:
        await message.edit_text(text, **kwargs)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return
        raise


PAGE_SIZE = 8
PROCESS_STARTED_AT = datetime.utcnow()

STATUS_LABELS = {
    "scheduled": "🟢 Запланированные",
    "ready": "🟡 Готовые к отправке",
    "sent": "📤 Отправленные",
    "cancelled": "❌ Отменённые",
}

ANALYSIS_STATUS_LABELS = {
    "new": "🆕 Новые",
    "paid": "💳 Оплаченные",
    "in_progress": "🛠 В работе",
    "done": "✅ Готовые",
    "cancelled": "❌ Отменённые",
}
ANALYSIS_STATUS_ORDER = tuple(ANALYSIS_STATUS_LABELS)


class UploadStates(StatesGroup):
    waiting_for_file = State()


class AdminSettingsStates(StatesGroup):
    waiting_for_analysis_payment_details = State()
    waiting_for_analysis_specialist_contact = State()
    waiting_for_subscription_payment_details = State()
    waiting_for_subscription_specialist_contact = State()


class UserSearchStates(StatesGroup):
    waiting_for_telegram_id = State()


def is_admin_user(user_id: int | None) -> bool:
    return bool(user_id and user_id in get_settings().admin_ids)


def is_admin(message: Message) -> bool:
    return is_admin_user(message.from_user.id if message.from_user else None)


def remove_uploaded_file(path: Path) -> bool:
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return True


def directory_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                continue
    return total


def format_bytes(value: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    amount = float(max(value, 0))
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(amount)} {unit}"
            return f"{amount:.1f} {unit}"
        amount /= 1024


def storage_usage_lines(settings) -> list[str]:
    db_path = sqlite_database_path(settings.database_url)
    db_size = directory_size_bytes(db_path) if db_path is not None else 0
    db_text = format_bytes(db_size) if db_path is not None else "\u0432\u043d\u0435\u0448\u043d\u044f\u044f \u0411\u0414"
    backup = latest_sqlite_backup(settings.data_dir)
    backup_text = (
        f"{backup.created_at:%d.%m.%Y %H:%M} - {format_bytes(backup.size_bytes)}"
        if backup is not None
        else "\u043d\u0435\u0442"
    )
    return [
        "\u0414\u0438\u0441\u043a:",
        f"data: {format_bytes(directory_size_bytes(settings.data_dir))}",
        f"uploads: {format_bytes(directory_size_bytes(settings.uploads_dir))}",
        f"SQLite: {db_text}",
        f"Backup SQLite: {backup_text}",
    ]


def format_uptime(started_at: datetime, now: datetime | None = None) -> str:
    now = now or datetime.utcnow()
    seconds = max(0, int((now - started_at).total_seconds()))
    days, seconds = divmod(seconds, 24 * 60 * 60)
    hours, seconds = divmod(seconds, 60 * 60)
    minutes, _ = divmod(seconds, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days} \u0434")
    if hours or days:
        parts.append(f"{hours} \u0447")
    parts.append(f"{minutes} \u043c\u0438\u043d")
    return " ".join(parts)


def maintenance_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="\U0001f4be Backup \u0441\u0435\u0439\u0447\u0430\u0441", callback_data="maint:backup")],
        [InlineKeyboardButton(text="\U0001f504 \u041e\u0431\u043d\u043e\u0432\u0438\u0442\u044c", callback_data="maint:refresh")],
    ])


def format_maintenance_text(settings, *, started_at: datetime = PROCESS_STARTED_AT, now: datetime | None = None) -> str:
    db_path = sqlite_database_path(settings.database_url)
    db_text = "SQLite" if db_path is not None else "\u0432\u043d\u0435\u0448\u043d\u044f\u044f \u0411\u0414"
    backup = latest_sqlite_backup(settings.data_dir)
    backup_text = (
        f"{backup.created_at:%d.%m.%Y %H:%M} - {format_bytes(backup.size_bytes)}"
        if backup is not None
        else "\u043d\u0435\u0442"
    )
    enabled = "\u0432\u043a\u043b\u044e\u0447\u0435\u043d" if settings.sqlite_backup_enabled else "\u0432\u044b\u043a\u043b\u044e\u0447\u0435\u043d"
    lines = [
        "\U0001f6e0 \u041e\u0431\u0441\u043b\u0443\u0436\u0438\u0432\u0430\u043d\u0438\u0435",
        "",
        f"\u0421\u0442\u0430\u0442\u0443\u0441: \u0431\u043e\u0442 \u0437\u0430\u043f\u0443\u0449\u0435\u043d",
        f"Uptime: {format_uptime(started_at, now)}",
        f"\u0411\u0430\u0437\u0430: {db_text}",
        f"Auto backup: {enabled}",
        f"\u0418\u043d\u0442\u0435\u0440\u0432\u0430\u043b: {settings.sqlite_backup_interval_hours} \u0447",
        f"\u0425\u0440\u0430\u043d\u0438\u0442\u044c \u043a\u043e\u043f\u0438\u0439: {settings.sqlite_backup_keep}",
        f"\u041f\u043e\u0441\u043b\u0435\u0434\u043d\u0438\u0439 backup: {backup_text}",
        "",
        *storage_usage_lines(settings),
    ]
    return "\n".join(lines)




def _local_dt(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(ZoneInfo(get_settings().timezone))


def _fmt_dt(value: datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    local = _local_dt(value)
    return local.strftime(fmt) if local else "—"


def _signal_group_value(signal_or_payload: ScheduledSignal | dict | None) -> str | None:
    if isinstance(signal_or_payload, ScheduledSignal):
        payload = signal_or_payload.signal_payload or {}
    elif isinstance(signal_or_payload, dict):
        payload = signal_or_payload
    else:
        payload = {}
    value = payload.get("signal_group")
    if value is None:
        return None
    return str(value).strip().lower()


def signal_group_label(signal_or_payload: ScheduledSignal | dict | None) -> str:
    labels = {
        "vip": "VIP",
        "all": "Все сигналы",
    }
    value = _signal_group_value(signal_or_payload)
    return labels.get(value, "—")


def signal_group_short_label(signal_or_payload: ScheduledSignal | dict | None) -> str:
    labels = {
        "vip": "VIP",
        "all": "ALL",
    }
    value = _signal_group_value(signal_or_payload)
    return labels.get(value, "—")


def summarize_import_decision_logs(logs: list[SignalDecisionLog]) -> tuple[dict[str, int], dict[str, int]]:
    group_counts: Counter[str] = Counter({"vip": 0, "all": 0, "unknown": 0})
    rejection_reasons: Counter[str] = Counter()
    for log in logs:
        if log.suitable:
            group = _signal_group_value(log.decision_payload if isinstance(log.decision_payload, dict) else None)
            group_counts[group if group in {"vip", "all"} else "unknown"] += 1
        else:
            reason = str(log.reason or "\u041f\u0440\u0438\u0447\u0438\u043d\u0430 \u043d\u0435 \u0443\u043a\u0430\u0437\u0430\u043d\u0430").strip()
            rejection_reasons[reason or "\u041f\u0440\u0438\u0447\u0438\u043d\u0430 \u043d\u0435 \u0443\u043a\u0430\u0437\u0430\u043d\u0430"] += 1
    return dict(group_counts), dict(rejection_reasons)


def format_import_signal_groups(group_counts: dict[str, int]) -> str:
    vip = group_counts.get("vip", 0)
    all_signals = group_counts.get("all", 0)
    unknown = group_counts.get("unknown", 0)
    total = vip + all_signals + unknown
    text = f"\u041d\u0430\u0439\u0434\u0435\u043d\u043e \u0441\u0438\u0433\u043d\u0430\u043b\u043e\u0432: {total} (VIP: {vip}, ALL: {all_signals})"
    if unknown:
        text += f", \u0431\u0435\u0437 \u0433\u0440\u0443\u043f\u043f\u044b: {unknown}"
    return text


def format_top_rejection_reasons(rejection_reasons: dict[str, int], *, limit: int = 5) -> str:
    if not rejection_reasons:
        return "\u041f\u0440\u0438\u0447\u0438\u043d\u044b \u043e\u0442\u043a\u043b\u043e\u043d\u0435\u043d\u0438\u0439: \u043d\u0435\u0442"
    items = sorted(rejection_reasons.items(), key=lambda item: (-item[1], item[0]))[:limit]
    lines = ["\u041f\u0440\u0438\u0447\u0438\u043d\u044b \u043e\u0442\u043a\u043b\u043e\u043d\u0435\u043d\u0438\u0439:"]
    lines.extend(f"- {reason}: {count}" for reason, count in items)
    extra = len(rejection_reasons) - len(items)
    if extra > 0:
        lines.append(f"- \u0438 \u0435\u0449\u0451 {extra}")
    return "\n".join(lines)


def format_import_warnings(warnings: list[str], *, limit: int = 5) -> str:
    if not warnings:
        return ""
    preview = "\n".join(f"- {item}" for item in warnings[:limit])
    suffix = f"\n- \u0438 \u0435\u0449\u0451 {len(warnings) - limit}" if len(warnings) > limit else ""
    return f"\u041f\u0440\u0435\u0434\u0443\u043f\u0440\u0435\u0436\u0434\u0435\u043d\u0438\u044f ({len(warnings)}):\n{preview}{suffix}"


def format_schedule_warning_lines(items: list[SignalListItem]) -> list[str]:
    lines: list[str] = []
    for item in items:
        lead = item.lead_minutes if item.lead_minutes is not None else "-"
        lines.append(
            f"#{item.id}: {_fmt_dt(item.send_at)} → матч {_fmt_dt(item.match_start_at)} "
            f"({lead} мин), {item.player_1} - {item.player_2}: {item.schedule_warning}"
        )
    return lines


def format_latest_import_text(
    batch: ImportBatch,
    group_counts: dict[str, int],
    rejection_reasons: dict[str, int],
    warnings: list[str] | None = None,
) -> str:
    lines = [
        "\U0001f4cb \u041f\u043e\u0441\u043b\u0435\u0434\u043d\u044f\u044f \u0437\u0430\u0433\u0440\u0443\u0437\u043a\u0430",
        "",
        f"\u0424\u0430\u0439\u043b: {batch.file_name}",
        f"\u0421\u0442\u0430\u0442\u0443\u0441: {batch.status}",
        f"\u0414\u0430\u0442\u0430: {_fmt_dt(batch.created_at, '%d.%m.%Y %H:%M:%S')}",
        f"\u0417\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u0430: {_fmt_dt(batch.finished_at, '%d.%m.%Y %H:%M:%S')}",
        "",
        f"\u0421\u0442\u0440\u043e\u043a \u0432 \u043b\u0438\u0441\u0442\u0435: {batch.total_rows}",
        f"\u0420\u0430\u0441\u043f\u043e\u0437\u043d\u0430\u043d\u043e \u043c\u0430\u0442\u0447\u0435\u0439: {batch.parsed_matches}",
        f"\u041d\u043e\u0432\u044b\u0445: {batch.inserted_matches}",
        f"\u041e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u043e: {batch.updated_matches}",
        f"\u041e\u0442\u0441\u0443\u0442\u0441\u0442\u0432\u0443\u044e\u0442 \u0432 \u0441\u0432\u0435\u0436\u0435\u0439 \u0442\u0430\u0431\u043b\u0438\u0446\u0435: {batch.missing_matches}",
        "",
        format_import_signal_groups(group_counts),
        "",
        format_top_rejection_reasons(rejection_reasons),
    ]
    warning_text = format_import_warnings(warnings or [])
    if warning_text:
        lines.extend(["", warning_text])
    return "\n".join(lines)


def _format_group_result_line(group: str, counter) -> str:
    return (
        f"{signal_group_title(group)}: ✅ {counter.won} / ❌ {counter.lost} / "
        f"↩️ {counter.void} / ❔ {counter.unknown} · WR {format_winrate(counter.winrate)}"
    )


def _format_tariff_result_line(tariff: str, counter) -> str:
    return (
        f"{signal_tariff_title(tariff)}: ✅ {counter.won} / ❌ {counter.lost} / "
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


SIGNAL_GROUP_FILTER_LABELS = {
    "any": "Все",
    "vip": "VIP",
    "all": "ALL",
}


def _signal_list_callback(status: str, page: int, group_filter: str = "any") -> str:
    if group_filter == "any":
        return f"sig:list:{status}:{page}"
    return f"sig:list:{status}:{page}:{group_filter}"


def signal_list_keyboard(
    items: list[tuple[ScheduledSignal, Match, SignalResult | None]],
    status: str,
    page: int,
    total: int,
    group_filter: str = "any",
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    rows.append([
        InlineKeyboardButton(
            text=("* " if group_filter == key else "") + label,
            callback_data=_signal_list_callback(status, 0, key),
        )
        for key, label in SIGNAL_GROUP_FILTER_LABELS.items()
    ])
    for signal, match, result in items:
        side = signal.signal_payload.get("side") if signal.signal_payload else None
        side_text = f"П{side}" if side in (1, 2) else "—"
        result_text = result_short_label(result.status if result else None)
        group_text = signal_group_short_label(signal)
        rows.append([
            InlineKeyboardButton(
                text=f"{match.match_time} · {group_text} · {match.player_1} — {match.player_2} · {side_text} · {result_text}",
                callback_data=f"sig:view:{signal.id}:{status}:{page}",
            )
        ])
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=_signal_list_callback(status, page - 1, group_filter)))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=_signal_list_callback(status, page + 1, group_filter)))
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
        group_text = signal_group_short_label(signal)
        rows.append([
            InlineKeyboardButton(
                text=f"{match.match_time} · {group_text} · {match.player_1} — {match.player_2} · {side_text} · {result_text}",
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
    group = signal_group_label(log.decision_payload or {})
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
        f"Тип: {group}",
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
        f"Тип сигнала: {signal_group_label(signal)}",
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
        group = signal_group_short_label(payload)
        sent_at = _fmt_dt(signal.sent_at or signal.send_at, "%d.%m %H:%M")
        counts = delivery_counts.get(signal.id, {})
        lines.extend([
            f"• {sent_at} · {result_short_label(result.status if result else None)} {result_source_label(result.source if result else None)} · {group} · {level} · {side_text}",
            f"  {match.player_1} — {match.player_2}",
            f"  доставки: ✅ {counts.get('sent', 0)} / ❌ {counts.get('failed', 0)} / ⏳ {counts.get('pending', 0)}",
        ])
    return "\n".join(lines)[:3900]

def _signal_group_filter_condition(group_filter: str):
    if group_filter == "vip":
        return ScheduledSignal.signal_payload["signal_group"].as_string() == "vip"
    if group_filter == "all":
        return ScheduledSignal.signal_payload["signal_group"].as_string() == "all"
    return None


def format_signals_dashboard_text(counts: dict[str, int], group_counts: dict[str, int]) -> str:
    total = sum(counts.values())
    return (
        "\U0001f4ca <b>\u0421\u0438\u0433\u043d\u0430\u043b\u044b</b>\n\n"
        f"\u0412\u0441\u0435\u0433\u043e \u0437\u0430\u043f\u0438\u0441\u0435\u0439: {total}\n"
        f"\U0001f7e2 \u0417\u0430\u043f\u043b\u0430\u043d\u0438\u0440\u043e\u0432\u0430\u043d\u043e: {counts.get('scheduled', 0)}\n"
        f"\U0001f7e1 \u0413\u043e\u0442\u043e\u0432\u043e \u043a \u043e\u0442\u043f\u0440\u0430\u0432\u043a\u0435: {counts.get('ready', 0)}\n"
        f"\U0001f4e4 \u041e\u0442\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u043e: {counts.get('sent', 0)}\n"
        f"\u274c \u041e\u0442\u043c\u0435\u043d\u0435\u043d\u043e: {counts.get('cancelled', 0)}\n\n"
        "\u041f\u043e \u0442\u0438\u043f\u0430\u043c:\n"
        f"VIP: {group_counts.get('vip', 0)}\n"
        f"ALL: {group_counts.get('all', 0)}\n"
        f"\u0411\u0435\u0437 \u0442\u0438\u043f\u0430: {group_counts.get('unknown', 0)}"
    )


async def get_signal_counts() -> dict[str, int]:
    async with SessionFactory() as session:
        rows = (await session.execute(
            select(ScheduledSignal.status, func.count(ScheduledSignal.id)).group_by(ScheduledSignal.status)
        )).all()
    return dict(rows)


async def get_signal_group_counts() -> dict[str, int]:
    counts = {"vip": 0, "all": 0, "unknown": 0}
    async with SessionFactory() as session:
        payloads = (await session.scalars(select(ScheduledSignal.signal_payload))).all()
    for payload in payloads:
        group = _signal_group_value(payload if isinstance(payload, dict) else None)
        if group in {"vip", "all"}:
            counts[group] += 1
        else:
            counts["unknown"] += 1
    return counts


async def show_dashboard(target: Message | CallbackQuery) -> None:
    counts = await get_signal_counts()
    group_counts = await get_signal_group_counts()
    text = format_signals_dashboard_text(counts, group_counts)
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
            schedule_warning_items = await collect_import_signal_schedule_warnings(session, summary.batch_id, settings=settings)
    except Exception as exc:
        await notify_admins(
            message.bot,
            settings.admin_ids,
            format_import_error_admin_text(safe_name, message.from_user.id, exc),
            exclude_ids=[message.from_user.id],
        )
        await message.answer(f"Ошибка обработки файла: {exc}")
        return
    finally:
        await state.clear()
        remove_uploaded_file(destination)
    lines = [
        "\u2705 \u0417\u0430\u0433\u0440\u0443\u0437\u043a\u0430 \u0437\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u0430",
        "",
        f"\u0421\u0442\u0440\u043e\u043a \u0432 \u043b\u0438\u0441\u0442\u0435: {summary.total_rows}",
        f"\u0420\u0430\u0441\u043f\u043e\u0437\u043d\u0430\u043d\u043e \u043c\u0430\u0442\u0447\u0435\u0439: {summary.parsed_matches}",
        f"\u041d\u043e\u0432\u044b\u0445 \u043c\u0430\u0442\u0447\u0435\u0439: {summary.inserted_matches}",
        f"\u041e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u043e \u043c\u0430\u0442\u0447\u0435\u0439: {summary.updated_matches}",
        f"\u041e\u0442\u0441\u0443\u0442\u0441\u0442\u0432\u0443\u044e\u0442 \u0432 \u0441\u0432\u0435\u0436\u0435\u0439 \u0442\u0430\u0431\u043b\u0438\u0446\u0435: {summary.missing_matches}",
        "",
        format_import_signal_groups(summary.scheduled_by_group),
        f"\u041e\u0442\u043c\u0435\u043d\u0435\u043d\u043e \u0441\u0438\u0433\u043d\u0430\u043b\u043e\u0432: {summary.cancelled_signals}",
        "",
        format_top_rejection_reasons(summary.rejection_reasons),
    ]
    warning_text = format_import_warnings(summary.warnings)
    if warning_text:
        lines.extend(["", warning_text])
    schedule_warning_lines = format_schedule_warning_lines(schedule_warning_items)
    if schedule_warning_lines:
        lines.extend(["", "⚠️ Проблемы расписания сигналов:"])
        lines.extend(f"• {item}" for item in schedule_warning_lines[:5])
        if len(schedule_warning_lines) > 5:
            lines.append(f"• …и ещё {len(schedule_warning_lines) - 5}")
    result_text = "\n".join(lines)
    await notify_admins(
        message.bot,
        settings.admin_ids,
        format_import_success_admin_text(summary, safe_name, message.from_user.id, schedule_warning_lines),
        exclude_ids=[message.from_user.id],
    )
    await message.answer(result_text, reply_markup=admin_menu())



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
    parts = callback.data.split(":")
    _, _, status, page_raw, *rest = parts
    page = max(0, int(page_raw))
    group_filter = rest[0] if rest else "any"
    if group_filter not in SIGNAL_GROUP_FILTER_LABELS:
        group_filter = "any"
    group_condition = _signal_group_filter_condition(group_filter)
    async with SessionFactory() as session:
        count_query = select(func.count(ScheduledSignal.id)).where(ScheduledSignal.status == status)
        list_query = (
            select(ScheduledSignal, Match, SignalResult)
            .join(Match, Match.id == ScheduledSignal.match_id)
            .outerjoin(SignalResult, SignalResult.signal_id == ScheduledSignal.id)
            .where(ScheduledSignal.status == status)
        )
        if group_condition is not None:
            count_query = count_query.where(group_condition)
            list_query = list_query.where(group_condition)
        total = int(await session.scalar(count_query) or 0)
        rows = (await session.execute(
            list_query
            .order_by(ScheduledSignal.send_at.asc())
            .offset(page * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )).all()
    label = STATUS_LABELS.get(status, status)
    group_label = SIGNAL_GROUP_FILTER_LABELS[group_filter]
    if not rows:
        text = f"{label} · {group_label}\n\nСписок пуст."
    else:
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        text = f"{label} · {group_label}\n\nСтраница {page + 1} из {pages}. Выберите матч:"
    await callback.message.edit_text(text, reply_markup=signal_list_keyboard(rows, status, page, total, group_filter))
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
    async with SessionFactory() as session:
        signal = await session.get(ScheduledSignal, signal_id)
        match = await session.get(Match, signal.match_id) if signal else None
        if signal is not None:
            await _refresh_signal_message_from_match(session, signal, match)
            await session.commit()
    if signal is None:
        await callback.answer("Сигнал не найден", show_alert=True)
        return
    header = (
        f"Результат: {result_full_label(result)}\n"
        f"Тип: {signal_group_label(signal)}\n"
        f"Уровень: {(signal.signal_payload or {}).get('level') or '—'}\n\n"
    )
    text = header + (signal.message_text or "Текст сигнала отсутствует")
    await callback.message.edit_text(
        text,
        reply_markup=signal_detail_keyboard(signal.id, status, page),
        disable_web_page_preview=True,
        parse_mode="HTML",
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
        if signal is not None:
            await _refresh_signal_message_from_match(session, signal)
            await session.commit()
    if signal is None or not signal.message_text:
        await callback.answer("Текст сигнала не найден", show_alert=True)
        return
    await callback.message.answer("🧪 Тестовая отправка администратору:\n\n" + signal.message_text, parse_mode="HTML")
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
            signal.send_at = to_utc_naive(match.match_start_at) - timedelta(minutes=lead_minutes)
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
        if batch is not None:
            logs = list((await session.scalars(
                select(SignalDecisionLog).where(SignalDecisionLog.import_batch_id == batch.id)
            )).all())
        else:
            logs = []
    if batch is None:
        await message.answer("\u0417\u0430\u0433\u0440\u0443\u0437\u043e\u043a \u0435\u0449\u0451 \u043d\u0435 \u0431\u044b\u043b\u043e.")
        return
    group_counts, rejection_reasons = summarize_import_decision_logs(logs)
    warnings = batch.error_text.splitlines() if batch.error_text else []
    await message.answer(format_latest_import_text(batch, group_counts, rejection_reasons, warnings))



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
            await safe_edit_text(target.message, text, reply_markup=markup)
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

async def show_maintenance(target: Message | CallbackQuery) -> None:
    settings = get_settings()
    text = format_maintenance_text(settings)
    markup = maintenance_keyboard()
    if isinstance(target, CallbackQuery):
        if target.message:
            await safe_edit_text(target.message, text, reply_markup=markup)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


@router.message(F.text == "🛠 Обслуживание")
async def maintenance_info(message: Message) -> None:
    if not is_admin(message):
        return
    await show_maintenance(message)


@router.callback_query(F.data == "maint:refresh")
async def maintenance_refresh_callback(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id):
        return
    await show_maintenance(callback)


@router.callback_query(F.data == "maint:backup")
async def maintenance_backup_callback(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id):
        return
    settings = get_settings()
    try:
        result = create_sqlite_backup(settings.database_url, settings.data_dir, keep=settings.sqlite_backup_keep)
    except (FileNotFoundError, ValueError) as error:
        await callback.answer(str(error), show_alert=True)
        return
    await callback.answer(
        f"Backup \u0441\u043e\u0437\u0434\u0430\u043d: {result.created.path.name} ({format_bytes(result.created.size_bytes)})",
        show_alert=True,
    )
    await show_maintenance(callback)


def format_admin_statistics_text(
    *,
    imports: int,
    users: int,
    signal_counts: dict[str, int],
    delivery_counts: dict[str, int],
    delivered_signal_count: int,
    result_summary,
    tariff_text: str,
    next_text: str,
    stats_correction: dict[str, int] | None = None,
) -> str:
    total_signals = sum(signal_counts.values())
    stats_total = result_summary.total_sent
    excluded_from_stats = max(total_signals - stats_total, 0)
    delivered_messages = delivery_counts.get("sent", 0)
    failed_deliveries = delivery_counts.get("failed", 0)
    correction = stats_correction or {}
    correction_won = max(int(correction.get("won", 0)), 0)
    correction_lost = max(int(correction.get("lost", 0)), 0)
    correction_void = max(int(correction.get("void", 0)), 0)
    correction_unknown = max(int(correction.get("unknown", 0)), 0)
    correction_total = correction_won + correction_lost + correction_void + correction_unknown
    official_won = max(result_summary.overall.won - correction_won, 0)
    official_lost = max(result_summary.overall.lost - correction_lost, 0)
    official_void = max(result_summary.overall.void - correction_void, 0)
    official_unknown = max(result_summary.overall.unknown - correction_unknown, 0)
    official_total = max(stats_total - correction_total, 0)
    official_evaluated = official_won + official_lost + official_void + official_unknown
    if official_evaluated > official_total:
        official_total = official_evaluated
    official_unrated = max(official_total - official_evaluated, 0)
    official_winrate = None
    if official_won + official_lost:
        official_winrate = official_won / (official_won + official_lost) * 100

    return (
        "📈 Статистика\n\n"
        f"Сегодня импортов: {imports}\nАктивных пользователей: {users}\n\n"
        "Официальная статистика:\n"
        f"Сигналов с результатом: {official_evaluated}\n"
        f"Всего в выборке: {official_total}\n"
        f"✅ Зашло: {official_won}\n"
        f"❌ Не зашло: {official_lost}\n"
        f"↩️ Возврат: {official_void}\n"
        f"❔ Неизвестно: {official_unknown}\n"
        f"Без результата: {official_unrated}\n"
        f"Процент захода: {format_winrate(official_winrate)}\n"
        f"Корректировка: исключено {correction_total}\n\n"
        "Сигналы в базе:\n"
        f"Всего: {total_signals}\n"
        f"Запланировано: {signal_counts.get('scheduled', 0)}\n"
        f"Готово: {signal_counts.get('ready', 0)}\n"
        f"Отправлено: {signal_counts.get('sent', 0)}\n"
        f"Отменено: {signal_counts.get('cancelled', 0)}\n\n"
        "Выборка статистики:\n"
        "Считаются только сигналы со статусом «отправлено».\n"
        f"Сигналов в статистике: {stats_total}\n"
        f"Исключено из статистики: {excluded_from_stats}\n\n"
        "Доставки пользователям:\n"
        f"Уникальных сигналов доставлено: {delivered_signal_count}\n"
        f"Всего доставок: {delivered_messages}\n"
        f"Ошибок доставки: {failed_deliveries}\n\n"
        f"Следующий сигнал: {next_text}"
    )

@router.message(F.text == "📈 Статистика")
async def admin_statistics(message: Message) -> None:
    if not is_admin(message):
        return
    settings = get_settings()
    today = datetime.now().date()
    start = datetime.combine(today, datetime.min.time())
    end = start + timedelta(days=1)
    async with SessionFactory() as session:
        imports = int(await session.scalar(select(func.count(ImportBatch.id)).where(ImportBatch.created_at >= start, ImportBatch.created_at < end)) or 0)
        users = int(await session.scalar(select(func.count(User.id)).where(User.is_active.is_(True))) or 0)
        counts = dict((await session.execute(select(ScheduledSignal.status, func.count(ScheduledSignal.id)).group_by(ScheduledSignal.status))).all())
        delivery_counts = dict((await session.execute(select(SignalDelivery.status, func.count(SignalDelivery.id)).group_by(SignalDelivery.status))).all())
        delivered_signal_count = int(await session.scalar(
            select(func.count(func.distinct(SignalDelivery.signal_id)))
            .where(SignalDelivery.status == "sent")
        ) or 0)
        sent_payload_rows = (
            await session.execute(
                select(ScheduledSignal.signal_payload, Match, SignalDecisionLog.suitable)
                .join(Match, Match.id == ScheduledSignal.match_id)
                .outerjoin(
                    SignalDecisionLog,
                    (SignalDecisionLog.match_id == ScheduledSignal.match_id)
                    & (SignalDecisionLog.import_batch_id == ScheduledSignal.source_import_id),
                )
                .where(ScheduledSignal.status == "sent")
            )
        ).all()
        eligible_sent_total = sum(
            1 for payload, match, decision_suitable in sent_payload_rows
            if signal_stats_eligible(match, decision_suitable=decision_suitable)
        )
        result_rows = [
            (payload, status)
            for payload, status, match, decision_suitable in (await session.execute(
                select(ScheduledSignal.signal_payload, SignalResult.status, Match, SignalDecisionLog.suitable)
                .join(Match, Match.id == ScheduledSignal.match_id)
                .join(SignalResult, SignalResult.signal_id == ScheduledSignal.id)
                .outerjoin(
                    SignalDecisionLog,
                    (SignalDecisionLog.match_id == ScheduledSignal.match_id)
                    & (SignalDecisionLog.import_batch_id == ScheduledSignal.source_import_id),
                )
                .where(ScheduledSignal.status == "sent")
            )).all()
            if signal_stats_eligible(match, decision_suitable=decision_suitable)
        ]
        next_signal = (await session.execute(
            select(ScheduledSignal, Match).join(Match, Match.id == ScheduledSignal.match_id)
            .where(ScheduledSignal.status == "scheduled")
            .order_by(ScheduledSignal.send_at.asc()).limit(1)
        )).first()
    next_text = "нет"
    if next_signal:
        signal, match = next_signal
        next_text = f"{_fmt_dt(signal.send_at, '%d.%m %H:%M')} · {match.player_1} — {match.player_2}"

    sent_total = eligible_sent_total
    result_summary = summarize_results(result_rows, total_sent=sent_total)
    tariff_lines = []
    for tariff in SIGNAL_TARIFF_ORDER:
        if tariff in result_summary.by_tariff:
            tariff_lines.append(_format_tariff_result_line(tariff, result_summary.by_tariff[tariff]))
    for tariff in sorted(set(result_summary.by_tariff) - set(SIGNAL_TARIFF_ORDER)):
        tariff_lines.append(_format_tariff_result_line(tariff, result_summary.by_tariff[tariff]))
    tariff_text = "\n".join(tariff_lines) if tariff_lines else "пока нет зафиксированных результатов"
    await message.answer(format_admin_statistics_text(
        imports=imports,
        users=users,
        signal_counts=counts,
        delivery_counts=delivery_counts,
        delivered_signal_count=delivered_signal_count,
        result_summary=result_summary,
        tariff_text=tariff_text,
        next_text=next_text,
        stats_correction={
            "won": settings.stats_correction_won,
            "lost": settings.stats_correction_lost,
            "void": settings.stats_correction_void,
            "unknown": settings.stats_correction_unknown,
        },
    ))

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
        if access.plan_id:
            remaining = "∞" if access.signals_remaining is None else str(access.signals_remaining)
            return f"paid · {access.plan_id} · осталось {remaining}{until}"
        return f"paid{until}"
    return access.access_type


def _subscription_status_label(status: str | None) -> str:
    return SUBSCRIPTION_STATUS_LABELS.get(status or "", status or "—")


def subscription_requests_dashboard_keyboard(counts: dict[str, int]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{label} ({counts.get(status, 0)})", callback_data=f"subadm:list:{status}:0")]
        for status, label in SUBSCRIPTION_STATUS_LABELS.items()
    ]
    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="subadm:dashboard")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subscription_requests_list_keyboard(
    items: list[tuple[SubscriptionRequest, User | None]],
    status: str,
    page: int,
    total: int,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for request, user in items:
        name = _user_name(user) if user else (request.username or str(request.telegram_id))
        created = _fmt_dt(request.created_at, "%d.%m %H:%M")
        rows.append([
            InlineKeyboardButton(
                text=f"#{request.id} · {created} · {request.plan_title} · {name}",
                callback_data=f"subadm:view:{request.id}:{status}:{page}",
            )
        ])
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"subadm:list:{status}:{page-1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"subadm:list:{status}:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ К заявкам", callback_data="subadm:dashboard")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subscription_request_detail_keyboard(request_id: int, current_status: str, list_status: str, page: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if current_status not in {"paid", "done"}:
        rows.append([
            InlineKeyboardButton(
                text="✅ Активировать и обработать",
                callback_data=f"subadm:activate:{request_id}:{list_status}:{page}",
            )
        ])
    status_buttons: list[InlineKeyboardButton] = []
    for status, label in SUBSCRIPTION_STATUS_LABELS.items():
        if status == current_status:
            continue
        status_buttons.append(InlineKeyboardButton(
            text=label,
            callback_data=f"subadm:status:{request_id}:{status}:{list_status}:{page}",
        ))
        if len(status_buttons) == 2:
            rows.append(status_buttons)
            status_buttons = []
    if status_buttons:
        rows.append(status_buttons)
    rows.append([InlineKeyboardButton(text="⬅️ К списку", callback_data=f"subadm:list:{list_status}:{page}")])
    rows.append([InlineKeyboardButton(text="⬅️ К заявкам", callback_data="subadm:dashboard")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_subscription_request_detail(request: SubscriptionRequest, user: User | None) -> str:
    username = f"@{request.username}" if request.username else "—"
    name = _user_name(user) if user else (request.username or "—")
    included = []
    if request.includes_vip:
        included.append("VIP")
    if request.includes_all_signals:
        included.append("все сигналы")
    if request.includes_analytics:
        included.append("аналитика")
    included_text = ", ".join(included) if included else "—"
    period_value = request.duration_days or request.duration_hours
    period_unit = "дней" if request.duration_days else "часов" if request.duration_hours else ""
    period_text = f"{period_value} {period_unit}" if period_value else "—"
    return (
        f"💳 Заявка на подписку #{request.id}\n\n"
        f"Статус: {_subscription_status_label(request.status)}\n"
        f"Создана: {_fmt_dt(request.created_at)}\n"
        f"Обновлена: {_fmt_dt(request.updated_at)}\n\n"
        f"Пользователь: {name}\n"
        f"ID Telegram: {request.telegram_id}\n"
        f"Имя пользователя: {username}\n\n"
        f"Тариф: {request.plan_title}\n"
        f"Условия: {request.plan_description}\n"
        f"Стоимость: {format_price(request.price_rub)}\n"
        f"Лимит сигналов: {request.signals_limit or '—'}\n"
        f"Период: {period_text}\n"
        f"Включено: {included_text}\n\n"
        f"Реквизиты: {request.payment_details or '—'}\n"
        f"Контакт: {request.specialist_contact or '—'}"
    )


def format_subscription_requests_dashboard_text(counts: dict[str, int], amounts: dict[str, int]) -> str:
    total = sum(counts.values())
    new_amount = amounts.get("new", 0)
    paid_amount = amounts.get("paid", 0)
    done_amount = amounts.get("done", 0)
    cancelled_amount = amounts.get("cancelled", 0)
    return (
        "💳 Заявки на подписку\n\n"
        f"Всего: {total}\n"
        f"Новые: {counts.get('new', 0)} · {format_price(new_amount)}\n"
        f"Оплаченные: {counts.get('paid', 0)} · {format_price(paid_amount)}\n"
        f"Обработанные: {counts.get('done', 0)} · {format_price(done_amount)}\n"
        f"Отменённые: {counts.get('cancelled', 0)} · {format_price(cancelled_amount)}\n\n"
        f"Оплачено + обработано: {format_price(paid_amount + done_amount)}\n"
        "Выберите статус, чтобы открыть список заявок."
    )


async def show_subscription_requests_dashboard(target: Message | CallbackQuery) -> None:
    async with SessionFactory() as session:
        rows = (await session.execute(
            select(
                SubscriptionRequest.status,
                func.count(SubscriptionRequest.id),
                func.coalesce(func.sum(SubscriptionRequest.price_rub), 0),
            ).group_by(SubscriptionRequest.status)
        )).all()
    counts = {status: int(count) for status, count, _ in rows}
    amounts = {status: int(amount or 0) for status, _, amount in rows}
    text = format_subscription_requests_dashboard_text(counts, amounts)
    markup = subscription_requests_dashboard_keyboard(counts)
    if isinstance(target, CallbackQuery):
        if target.message:
            await safe_edit_text(target.message, text, reply_markup=markup)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


async def show_subscription_requests_list(target: CallbackQuery | Message, status: str, page: int = 0) -> None:
    page = max(0, page)
    if status not in SUBSCRIPTION_STATUS_LABELS:
        if isinstance(target, CallbackQuery):
            await target.answer("Неизвестный статус", show_alert=True)
        return
    async with SessionFactory() as session:
        total = int(await session.scalar(
            select(func.count(SubscriptionRequest.id)).where(SubscriptionRequest.status == status)
        ) or 0)
        rows = list((await session.execute(
            select(SubscriptionRequest, User)
            .outerjoin(User, User.id == SubscriptionRequest.user_id)
            .where(SubscriptionRequest.status == status)
            .order_by(desc(SubscriptionRequest.created_at))
            .offset(page * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )).all())
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    text = (
        f"💳 Заявки на подписку: {_subscription_status_label(status)}\n\n"
        f"Всего: {total}\n"
        f"Страница {page + 1} из {pages}."
    )
    markup = subscription_requests_list_keyboard(rows, status, page, total)
    if isinstance(target, CallbackQuery):
        if target.message:
            await safe_edit_text(target.message, text, reply_markup=markup)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


async def show_subscription_request_detail(callback: CallbackQuery, request_id: int, list_status: str, page: int) -> None:
    if not callback.message:
        return
    async with SessionFactory() as session:
        row = (await session.execute(
            select(SubscriptionRequest, User)
            .outerjoin(User, User.id == SubscriptionRequest.user_id)
            .where(SubscriptionRequest.id == request_id)
        )).first()
    if row is None:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    request, user = row
    await callback.message.edit_text(
        format_subscription_request_detail(request, user),
        reply_markup=subscription_request_detail_keyboard(request.id, request.status, list_status, page),
    )
    await callback.answer()


def _analysis_status_label(status: str | None) -> str:
    return ANALYSIS_STATUS_LABELS.get(status or "", status or "—")


def analysis_dashboard_keyboard(counts: dict[str, int]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{label} ({counts.get(status, 0)})", callback_data=f"an:list:{status}:0")]
        for status, label in ANALYSIS_STATUS_LABELS.items()
    ]
    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="an:dashboard")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def analysis_list_keyboard(
    items: list[tuple[MatchAnalysisRequest, User | None]],
    status: str,
    page: int,
    total: int,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for request, user in items:
        name = _user_name(user) if user else (request.username or str(request.telegram_id))
        created = _fmt_dt(request.created_at, "%d.%m %H:%M")
        rows.append([
            InlineKeyboardButton(
                text=f"#{request.id} · {created} · {name}",
                callback_data=f"an:view:{request.id}:{status}:{page}",
            )
        ])

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"an:list:{status}:{page-1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"an:list:{status}:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ К заявкам", callback_data="an:dashboard")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def analysis_detail_keyboard(request_id: int, current_status: str, list_status: str, page: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if current_status in {"paid", "in_progress"}:
        rows.append([
            InlineKeyboardButton(
                text="📤 Выдать анализ клиенту",
                callback_data=f"an:issue:{request_id}:{list_status}:{page}",
            )
        ])
    status_buttons: list[InlineKeyboardButton] = []
    for status, label in ANALYSIS_STATUS_LABELS.items():
        if status == current_status:
            continue
        status_buttons.append(InlineKeyboardButton(
            text=label,
            callback_data=f"an:status:{request_id}:{status}:{list_status}:{page}",
        ))
        if len(status_buttons) == 2:
            rows.append(status_buttons)
            status_buttons = []
    if status_buttons:
        rows.append(status_buttons)
    rows.append([InlineKeyboardButton(text="⬅️ К списку", callback_data=f"an:list:{list_status}:{page}")])
    rows.append([InlineKeyboardButton(text="⬅️ К заявкам", callback_data="an:dashboard")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_analysis_request_detail(request: MatchAnalysisRequest, user: User | None) -> str:
    username = f"@{request.username}" if request.username else "?"
    name = _user_name(user) if user else (request.username or "?")
    return (
        f"🔎 Заявка на анализ #{request.id}\n\n"
        f"Статус: {_analysis_status_label(request.status)}\n"
        f"Создана: {_fmt_dt(request.created_at)}\n"
        f"Обновлена: {_fmt_dt(request.updated_at)}\n\n"
        f"Пользователь: {name}\n"
        f"ID Telegram: {request.telegram_id}\n"
        f"Имя пользователя: {username}\n\n"
        f"Матч:\n{request.match_title or request.match_text}\n\n"
        f"Оплата: {request.payment_details or '?'}\n"
        f"Контакт: {request.specialist_contact or '?'}"
    )



async def show_analysis_dashboard(target: Message | CallbackQuery) -> None:
    async with SessionFactory() as session:
        rows = (await session.execute(
            select(MatchAnalysisRequest.status, func.count(MatchAnalysisRequest.id)).group_by(MatchAnalysisRequest.status)
        )).all()
    counts = {status: int(count) for status, count in rows}
    total = sum(counts.values())
    text = (
        "🔎 Заявки на анализ\n\n"
        f"Всего: {total}\n"
        "Выберите статус, чтобы открыть список заявок."
    )
    markup = analysis_dashboard_keyboard(counts)
    if isinstance(target, CallbackQuery):
        if target.message:
            await safe_edit_text(target.message, text, reply_markup=markup)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


async def show_analysis_list(target: CallbackQuery | Message, status: str, page: int = 0) -> None:
    page = max(0, page)
    if status not in ANALYSIS_STATUS_LABELS:
        if isinstance(target, CallbackQuery):
            await target.answer("Неизвестный статус", show_alert=True)
        return

    async with SessionFactory() as session:
        total = int(await session.scalar(
            select(func.count(MatchAnalysisRequest.id)).where(MatchAnalysisRequest.status == status)
        ) or 0)
        rows = list((await session.execute(
            select(MatchAnalysisRequest, User)
            .outerjoin(User, User.id == MatchAnalysisRequest.user_id)
            .where(MatchAnalysisRequest.status == status)
            .order_by(desc(MatchAnalysisRequest.created_at))
            .offset(page * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )).all())

    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    text = (
        f"🔎 Заявки на анализ: {_analysis_status_label(status)}\n\n"
        f"Всего: {total}\n"
        f"Страница {page + 1} из {pages}."
    )
    markup = analysis_list_keyboard(rows, status, page, total)
    if isinstance(target, CallbackQuery):
        if target.message:
            await safe_edit_text(target.message, text, reply_markup=markup)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


async def show_analysis_detail(callback: CallbackQuery, request_id: int, list_status: str, page: int) -> None:
    if not callback.message:
        return
    async with SessionFactory() as session:
        row = (await session.execute(
            select(MatchAnalysisRequest, User)
            .outerjoin(User, User.id == MatchAnalysisRequest.user_id)
            .where(MatchAnalysisRequest.id == request_id)
        )).first()
    if row is None:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    request, user = row
    await safe_edit_text(
        callback.message,
        format_analysis_request_detail(request, user),
        reply_markup=analysis_detail_keyboard(request.id, request.status, list_status, page),
    )
    await callback.answer()


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
    rows.append([InlineKeyboardButton(text="🔎 Найти по ID Telegram", callback_data="usr:search")])
    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data=f"usr:list:{page}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def user_detail_keyboard(user_id: int, page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Выдать trial 9", callback_data=f"usr:trial:{user_id}:{page}")],
        [InlineKeyboardButton(text="💳 Выдать paid", callback_data=f"usr:paid:{user_id}:{page}")],
        [
            InlineKeyboardButton(text="💳 Заявки подписки", callback_data=f"usr:subreq:{user_id}:{page}"),
            InlineKeyboardButton(text="🔎 Заявки анализа", callback_data=f"usr:anreq:{user_id}:{page}"),
        ],
        [InlineKeyboardButton(text="⛔ Отключить доступ", callback_data=f"usr:disable:{user_id}:{page}")],
        [InlineKeyboardButton(text="⬅️ К пользователям", callback_data=f"usr:list:{page}")],
    ])


def _format_user_subscription_requests(requests: list[SubscriptionRequest]) -> str:
    if not requests:
        return "пока нет"
    lines = []
    for request in requests:
        lines.append(
            f"#{request.id} · {_subscription_status_label(request.status)} · "
            f"{request.plan_title} · {format_price(request.price_rub)}"
        )
    return "\n".join(lines)


def _format_user_analysis_requests(requests: list[MatchAnalysisRequest]) -> str:
    if not requests:
        return "пока нет"
    lines = []
    for request in requests:
        lines.append(f"#{request.id} · {_analysis_status_label(request.status)} · {_fmt_dt(request.created_at, '%d.%m %H:%M')}")
    return "\n".join(lines)


def format_user_detail(
    user: User,
    access: UserAccess | None,
    delivered: int,
    failed: int,
    subscription_requests: list[SubscriptionRequest] | None = None,
    analysis_requests: list[MatchAnalysisRequest] | None = None,
) -> str:
    username = f"@{user.username}" if user.username else "—"
    created = _fmt_dt(user.created_at)
    subscription_requests = subscription_requests or []
    analysis_requests = analysis_requests or []
    return (
        "👤 Пользователь\n\n"
        f"Имя: {_user_name(user)}\n"
        f"ID Telegram: {user.telegram_id}\n"
        f"Имя пользователя: {username}\n"
        f"Активен: {'да' if user.is_active else 'нет'}\n"
        f"Создан: {created}\n\n"
        f"Доступ: {_access_label(access)}\n"
        f"Тариф: {access.plan_id if access and access.plan_id else '—'}\n"
        f"Осталось платных сигналов: {access.signals_remaining if access and access.signals_remaining is not None else '—'}\n"
        f"Успешных доставок: {delivered}\n"
        f"Ошибок доставки: {failed}\n\n"
        f"Последние заявки на подписку:\n{_format_user_subscription_requests(subscription_requests)}\n\n"
        f"Последние заявки на анализ:\n{_format_user_analysis_requests(analysis_requests)}"
    )


def user_subscription_requests_keyboard(requests: list[SubscriptionRequest], user_id: int, page: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for request in requests:
        rows.append([
            InlineKeyboardButton(
                text=f"#{request.id} · {_subscription_status_label(request.status)} · {request.plan_title}",
                callback_data=f"subadm:view:{request.id}:{request.status}:0",
            )
        ])
    rows.append([InlineKeyboardButton(text="⬅️ К пользователю", callback_data=f"usr:view:{user_id}:{page}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def user_analysis_requests_keyboard(requests: list[MatchAnalysisRequest], user_id: int, page: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for request in requests:
        rows.append([
            InlineKeyboardButton(
                text=f"#{request.id} · {_analysis_status_label(request.status)} · {_fmt_dt(request.created_at, '%d.%m %H:%M')}",
                callback_data=f"an:view:{request.id}:{request.status}:0",
            )
        ])
    rows.append([InlineKeyboardButton(text="⬅️ К пользователю", callback_data=f"usr:view:{user_id}:{page}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
            await safe_edit_text(target.message, text, reply_markup=markup)
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
        subscription_requests = list((await session.scalars(
            select(SubscriptionRequest)
            .where(SubscriptionRequest.user_id == user.id)
            .order_by(desc(SubscriptionRequest.created_at))
            .limit(3)
        )).all())
        analysis_requests = list((await session.scalars(
            select(MatchAnalysisRequest)
            .where(MatchAnalysisRequest.user_id == user.id)
            .order_by(desc(MatchAnalysisRequest.created_at))
            .limit(3)
        )).all())
    await callback.message.edit_text(
        format_user_detail(user, access, delivered, failed, subscription_requests, analysis_requests),
        reply_markup=user_detail_keyboard(user_id, page),
    )
    await callback.answer()
@router.message(F.text == "💳 Заявки на подписку")
async def subscription_requests_info(message: Message) -> None:
    if not is_admin(message):
        return
    await show_subscription_requests_dashboard(message)


@router.callback_query(F.data == "subadm:dashboard")
async def subscription_requests_dashboard_callback(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id):
        return
    await show_subscription_requests_dashboard(callback)


@router.callback_query(F.data.startswith("subadm:list:"))
async def subscription_requests_list_callback(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id) or not callback.data:
        return
    _, _, status, page_raw = callback.data.split(":")
    await show_subscription_requests_list(callback, status, int(page_raw))


@router.callback_query(F.data.startswith("subadm:view:"))
async def subscription_request_view_callback(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id) or not callback.data:
        return
    _, _, request_id_raw, status, page_raw = callback.data.split(":")
    await show_subscription_request_detail(callback, int(request_id_raw), status, int(page_raw))


async def _activate_subscription_request(request_id: int) -> tuple[str | None, int | None]:
    activation_text: str | None = None
    user_telegram_id: int | None = None
    async with SessionFactory() as session:
        request = await session.get(SubscriptionRequest, request_id)
        if request is None:
            return None, None
        previous_status = request.status
        request.status = "done"
        request.updated_at = datetime.utcnow()
        user = await session.get(User, request.user_id)
        if user is not None:
            await grant_subscription_access(session, user, request)
            user_telegram_id = user.telegram_id
            if previous_status not in {"paid", "done"}:
                activation_text = format_subscription_activation_user_text(request)
        await session.commit()
    return activation_text, user_telegram_id


@router.callback_query(F.data.startswith("subadm:activate:"))
async def subscription_request_activate_callback(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id) or not callback.data:
        return
    _, _, request_id_raw, list_status, page_raw = callback.data.split(":")
    request_id = int(request_id_raw)
    page = int(page_raw)
    activation_text, user_telegram_id = await _activate_subscription_request(request_id)
    if activation_text is None and user_telegram_id is None:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    if activation_text and user_telegram_id is not None:
        try:
            await callback.bot.send_message(chat_id=user_telegram_id, text=activation_text)
        except Exception:
            pass
    await callback.answer("Подписка активирована и заявка обработана", show_alert=True)
    await show_subscription_request_detail(callback, request_id, list_status, page)


@router.callback_query(F.data.startswith("subadm:status:"))
async def subscription_request_status_callback(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id) or not callback.data:
        return
    _, _, request_id_raw, new_status, list_status, page_raw = callback.data.split(":")
    if new_status not in SUBSCRIPTION_STATUS_LABELS:
        await callback.answer("Неизвестный статус", show_alert=True)
        return
    request_id = int(request_id_raw)
    page = int(page_raw)
    activation_text: str | None = None
    user_telegram_id: int | None = None
    async with SessionFactory() as session:
        request = await session.get(SubscriptionRequest, request_id)
        if request is None:
            await callback.answer("Заявка не найдена", show_alert=True)
            return
        previous_status = request.status
        request.status = new_status
        request.updated_at = datetime.utcnow()
        if new_status in {"paid", "done"}:
            user = await session.get(User, request.user_id)
            if user is not None:
                await grant_subscription_access(session, user, request)
                user_telegram_id = user.telegram_id
                if previous_status not in {"paid", "done"}:
                    activation_text = format_subscription_activation_user_text(request)
        await session.commit()
    if activation_text and user_telegram_id is not None:
        try:
            await callback.bot.send_message(chat_id=user_telegram_id, text=activation_text)
        except Exception:
            pass
    await callback.answer(f"Статус изменён: {_subscription_status_label(new_status)}", show_alert=True)
    await show_subscription_request_detail(callback, request_id, list_status, page)


@router.message(F.text == "🔎 Заявки на анализ")
async def analysis_requests_info(message: Message) -> None:
    if not is_admin(message):
        return
    await show_analysis_dashboard(message)


@router.callback_query(F.data == "an:dashboard")
async def analysis_dashboard_callback(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id):
        return
    await show_analysis_dashboard(callback)


@router.callback_query(F.data.startswith("an:list:"))
async def analysis_list_callback(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id) or not callback.data:
        return
    _, _, status, page_raw = callback.data.split(":")
    await show_analysis_list(callback, status, int(page_raw))


@router.callback_query(F.data.startswith("an:view:"))
async def analysis_view_callback(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id) or not callback.data:
        return
    _, _, request_id_raw, status, page_raw = callback.data.split(":")
    await show_analysis_detail(callback, int(request_id_raw), status, int(page_raw))


@router.callback_query(F.data.startswith("an:work:"))
async def analysis_take_to_work_callback(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id) or not callback.data:
        return
    _, _, request_id_raw, list_status, page_raw = callback.data.split(":")
    request_id = int(request_id_raw)
    page = int(page_raw)
    status_text: str | None = None
    user_telegram_id: int | None = None
    async with SessionFactory() as session:
        request = await session.get(MatchAnalysisRequest, request_id)
        if request is None:
            await callback.answer("Заявка не найдена", show_alert=True)
            return
        previous_status = request.status
        request.status = "in_progress"
        request.updated_at = datetime.utcnow()
        if previous_status != "in_progress":
            status_text = format_analysis_status_user_text(request)
            user_telegram_id = request.telegram_id
        await session.commit()
    if status_text and user_telegram_id is not None:
        try:
            await callback.bot.send_message(chat_id=user_telegram_id, text=status_text)
        except Exception:
            pass
    await callback.answer("Заявка взята в работу", show_alert=True)
    await show_analysis_detail(callback, request_id, list_status, page)


@router.callback_query(F.data.startswith("an:status:"))
async def analysis_status_callback(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id) or not callback.data:
        return
    _, _, request_id_raw, new_status, list_status, page_raw = callback.data.split(":")
    if new_status not in ANALYSIS_STATUS_LABELS:
        await callback.answer("Неизвестный статус", show_alert=True)
        return
    request_id = int(request_id_raw)
    page = int(page_raw)
    status_text: str | None = None
    user_telegram_id: int | None = None
    async with SessionFactory() as session:
        request = await session.get(MatchAnalysisRequest, request_id)
        if request is None:
            await callback.answer("Заявка не найдена", show_alert=True)
            return
        previous_status = request.status
        request.status = new_status
        request.updated_at = datetime.utcnow()
    if status_text and user_telegram_id is not None:
        try:
            await callback.bot.send_message(chat_id=user_telegram_id, text=status_text)
        except Exception:
            pass
    await callback.answer(f"Статус изменён: {_analysis_status_label(new_status)}", show_alert=True)
    await show_analysis_detail(callback, request_id, list_status, page)


@router.callback_query(F.data.startswith("an:issue:"))
async def analysis_issue_callback(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id) or not callback.data:
        return
    parts = callback.data.split(":")
    if len(parts) == 3:
        _, _, request_id_raw = parts
        list_status = "paid"
        page = 0
    elif len(parts) == 5:
        _, _, request_id_raw, list_status, page_raw = parts
        page = int(page_raw)
    else:
        await callback.answer("Неизвестная команда", show_alert=True)
        return
    request_id = int(request_id_raw)
    async with SessionFactory() as session:
        request = await session.get(MatchAnalysisRequest, request_id)
        if request is None:
            await callback.answer("Заявка не найдена", show_alert=True)
            return
        match = await session.get(Match, request.match_id) if request.match_id is not None else None
        if match is None:
            await callback.answer("Матч не найден", show_alert=True)
            return
        analysis_text = build_match_analysis_text(match)
        user_telegram_id = request.telegram_id
    try:
        await callback.bot.send_message(chat_id=user_telegram_id, text=analysis_text)
    except Exception:
        await callback.answer("Не удалось отправить анализ клиенту", show_alert=True)
        return
    async with SessionFactory() as session:
        request = await session.get(MatchAnalysisRequest, request_id)
        if request is not None:
            request.status = "done"
            request.updated_at = datetime.utcnow()
            await session.commit()
    await callback.answer("Анализ выдан клиенту", show_alert=True)
    await show_analysis_detail(callback, request_id, list_status, page)


@router.message(F.text == "👥 Пользователи")
async def users_message_handler(message: Message) -> None:
    if not is_admin_user(message.from_user.id):
        return
    await show_users_list(message, 0)


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


@router.callback_query(F.data == "usr:search")
async def user_search_start_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin_user(callback.from_user.id):
        return
    await state.set_state(UserSearchStates.waiting_for_telegram_id)
    if callback.message:
        await callback.message.answer("Введите ID Telegram пользователя.")
    await callback.answer()


@router.message(UserSearchStates.waiting_for_telegram_id)
async def user_search_by_telegram_id(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        return
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("ID Telegram должен быть числом. Попробуйте ещё раз.")
        return
    telegram_id = int(raw)
    async with SessionFactory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    await state.clear()
    if user is None:
        await message.answer("Пользователь с таким ID Telegram не найден.")
        return
    await message.answer(
        f"Пользователь найден: {_user_name(user)}\nID Telegram: {user.telegram_id}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Открыть карточку", callback_data=f"usr:view:{user.id}:0")],
            [InlineKeyboardButton(text="⬅️ К пользователям", callback_data="usr:list:0")],
        ]),
    )


@router.callback_query(F.data.startswith("usr:subreq:"))
async def user_subscription_requests_callback(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id) or not callback.data:
        return
    _, _, user_id_raw, page_raw = callback.data.split(":")
    user_id = int(user_id_raw)
    page = int(page_raw)
    async with SessionFactory() as session:
        user = await session.get(User, user_id)
        if user is None:
            await callback.answer("Пользователь не найден", show_alert=True)
            return
        requests = list((await session.scalars(
            select(SubscriptionRequest)
            .where(SubscriptionRequest.user_id == user.id)
            .order_by(desc(SubscriptionRequest.created_at))
            .limit(10)
        )).all())
    text = f"💳 Заявки на подписку\n\nПользователь: {_user_name(user)}\nВсего показано: {len(requests)}"
    if callback.message:
        await callback.message.edit_text(text, reply_markup=user_subscription_requests_keyboard(requests, user_id, page))
    await callback.answer()


@router.callback_query(F.data.startswith("usr:anreq:"))
async def user_analysis_requests_callback(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id) or not callback.data:
        return
    _, _, user_id_raw, page_raw = callback.data.split(":")
    user_id = int(user_id_raw)
    page = int(page_raw)
    async with SessionFactory() as session:
        user = await session.get(User, user_id)
        if user is None:
            await callback.answer("Пользователь не найден", show_alert=True)
            return
        requests = list((await session.scalars(
            select(MatchAnalysisRequest)
            .where(MatchAnalysisRequest.user_id == user.id)
            .order_by(desc(MatchAnalysisRequest.created_at))
            .limit(10)
        )).all())
    text = f"🔎 Заявки на анализ\n\nПользователь: {_user_name(user)}\nВсего показано: {len(requests)}"
    if callback.message:
        await callback.message.edit_text(text, reply_markup=user_analysis_requests_keyboard(requests, user_id, page))
    await callback.answer()


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

def admin_settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Изменить реквизиты анализа", callback_data="admset:analysis_payment")],
        [InlineKeyboardButton(text="👤 Изменить контакт анализа", callback_data="admset:analysis_contact")],
        [InlineKeyboardButton(text="💳 Изменить реквизиты подписки", callback_data="admset:subscription_payment")],
        [InlineKeyboardButton(text="👤 Изменить контакт подписки", callback_data="admset:subscription_contact")],
        [InlineKeyboardButton(text="💾 Сделать backup SQLite", callback_data="admset:backup")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="admset:refresh")],
    ])


def format_admin_settings_text(
    settings,
    rules: dict,
    analysis_payment_details: str,
    analysis_specialist_contact: str,
    subscription_payment_details: str,
    subscription_specialist_contact: str,
) -> str:
    signal = rules.get("signal", {})
    levels = rules.get("levels", {})
    top_probability = levels.get("top", {}).get("min_probability", "-")
    strong_probability = levels.get("strong", {}).get("min_probability", "-")
    return (
        "⚙️ Настройки\n\n"
        f"Часовой пояс: {settings.timezone}\n"
        f"Отправка до матча: {signal.get('lead_minutes', '-')} минут\n"
        f"Минимум H2H (CP): {signal.get('min_h2h_games', '-')}\n"
        f"Минимальная форма Q/X: {signal.get('min_favorite_form', '-')}\n"
        f"VIP от: {top_probability}%\n"
        f"Все сигналы от: {strong_probability}%\n"
        f"Проверка очереди: каждые {settings.scheduler_interval_seconds} секунд\n"
        f"Максимальный Excel: {settings.max_upload_mb} МБ\n\n"
        "🔎 Анализ матча\n"
        f"Реквизиты: {analysis_payment_details}\n"
        f"Контакт специалиста: {analysis_specialist_contact}\n\n"
        "💳 Подписки\n"
        f"Реквизиты: {subscription_payment_details}\n"
        f"Контакт специалиста: {subscription_specialist_contact}\n\n"
        "Правила сигналов читаются из signal_rules.yaml. Реквизиты и контакты можно менять кнопками ниже."
    )

async def show_admin_settings(target: Message | CallbackQuery) -> None:
    settings = get_settings()
    rules = reload_signal_rules()
    async with SessionFactory() as session:
        analysis_config = await get_analysis_payment_config(session)
        subscription_config = await get_subscription_payment_config(session)
    text = format_admin_settings_text(
        settings,
        rules,
        analysis_config.payment_details,
        analysis_config.specialist_contact,
        subscription_config.payment_details,
        subscription_config.specialist_contact,
    )
    markup = admin_settings_keyboard()
    if isinstance(target, CallbackQuery):
        if target.message:
            await safe_edit_text(target.message, text, reply_markup=markup)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


@router.message(F.text == "⚙️ Настройки")
async def settings_info(message: Message) -> None:
    if not is_admin(message):
        return
    await show_admin_settings(message)


@router.callback_query(F.data == "admset:refresh")
async def admin_settings_refresh_callback(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id):
        return
    await show_admin_settings(callback)


@router.callback_query(F.data == "admset:backup")
async def admin_settings_backup_callback(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id):
        return
    settings = get_settings()
    try:
        result = create_sqlite_backup(settings.database_url, settings.data_dir, keep=settings.sqlite_backup_keep)
    except (FileNotFoundError, ValueError) as error:
        await callback.answer(str(error), show_alert=True)
        return

    await callback.answer(
        f"Backup \u0441\u043e\u0437\u0434\u0430\u043d: {result.created.path.name} ({format_bytes(result.created.size_bytes)})",
        show_alert=True,
    )
    await show_admin_settings(callback)


@router.callback_query(F.data == "admset:analysis_payment")
async def admin_edit_analysis_payment_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin_user(callback.from_user.id):
        return
    await state.set_state(AdminSettingsStates.waiting_for_analysis_payment_details)
    if callback.message:
        await callback.message.answer(
            "Напишите новые реквизиты для оплаты анализа матча.\n\n"
            "Они будут показаны пользователю после создания заявки."
        )
    await callback.answer()


@router.callback_query(F.data == "admset:analysis_contact")
async def admin_edit_analysis_contact_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin_user(callback.from_user.id):
        return
    await state.set_state(AdminSettingsStates.waiting_for_analysis_specialist_contact)
    if callback.message:
        await callback.message.answer(
            "Напишите контакт специалиста для анализа матча.\n\n"
            "Например: @ivanov или номер телефона."
        )
    await callback.answer()


@router.callback_query(F.data == "admset:subscription_payment")
async def admin_edit_subscription_payment_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin_user(callback.from_user.id):
        return
    await state.set_state(AdminSettingsStates.waiting_for_subscription_payment_details)
    if callback.message:
        await callback.message.answer(
            "Напишите новые реквизиты для оплаты подписки.\n\n"
            "Они будут показаны пользователю после создания заявки."
        )
    await callback.answer()


@router.callback_query(F.data == "admset:subscription_contact")
async def admin_edit_subscription_contact_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin_user(callback.from_user.id):
        return
    await state.set_state(AdminSettingsStates.waiting_for_subscription_specialist_contact)
    if callback.message:
        await callback.message.answer(
            "Напишите контакт специалиста для подписки.\n\n"
            "Например: @ivanov или номер телефона."
        )
    await callback.answer()


@router.message(AdminSettingsStates.waiting_for_analysis_payment_details)
async def admin_save_analysis_payment(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        return
    try:
        async with SessionFactory() as session:
            await set_bot_setting(session, ANALYSIS_PAYMENT_DETAILS_KEY, message.text or "", max_length=2000)
            await session.commit()
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.clear()
    await message.answer("Реквизиты для анализа обновлены.")
    await show_admin_settings(message)


@router.message(AdminSettingsStates.waiting_for_analysis_specialist_contact)
async def admin_save_analysis_contact(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        return
    try:
        async with SessionFactory() as session:
            await set_bot_setting(session, ANALYSIS_SPECIALIST_CONTACT_KEY, message.text or "", max_length=255)
            await session.commit()
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.clear()
    await message.answer("Контакт специалиста обновлён.")
    await show_admin_settings(message)


@router.message(AdminSettingsStates.waiting_for_subscription_payment_details)
async def admin_save_subscription_payment(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        return
    try:
        async with SessionFactory() as session:
            await set_bot_setting(session, SUBSCRIPTION_PAYMENT_DETAILS_KEY, message.text or "", max_length=2000)
            await session.commit()
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.clear()
    await message.answer("Реквизиты для подписки обновлены.")
    await show_admin_settings(message)


@router.message(AdminSettingsStates.waiting_for_subscription_specialist_contact)
async def admin_save_subscription_contact(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        return
    try:
        async with SessionFactory() as session:
            await set_bot_setting(session, SUBSCRIPTION_SPECIALIST_CONTACT_KEY, message.text or "", max_length=255)
            await session.commit()
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.clear()
    await message.answer("Контакт специалиста по подпискам обновлён.")
    await show_admin_settings(message)
