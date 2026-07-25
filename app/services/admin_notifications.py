from __future__ import annotations

import logging
from typing import Iterable, Protocol

logger = logging.getLogger(__name__)


class AdminNotificationBot(Protocol):
    async def send_message(self, *, chat_id: int, text: str, **kwargs: object) -> object:
        ...


async def notify_admins(
    bot: AdminNotificationBot,
    admin_ids: Iterable[int],
    text: str,
    *,
    exclude_ids: Iterable[int] = (),
) -> int:
    excluded = set(exclude_ids)
    delivered = 0
    for admin_id in dict.fromkeys(int(item) for item in admin_ids if int(item) not in excluded):
        try:
            await bot.send_message(chat_id=admin_id, text=text, disable_web_page_preview=True)
        except Exception as exc:
            logger.warning("Не удалось отправить уведомление админу %s: %s", admin_id, exc)
        else:
            delivered += 1
    return delivered


def format_import_success_admin_text(summary, file_name: str, uploaded_by: int) -> str:
    groups = getattr(summary, "scheduled_by_group", {}) or {}
    warnings = getattr(summary, "warnings", []) or []
    lines = [
        "✅ Excel загружен",
        "",
        f"Файл: {file_name}",
        f"Загрузил Telegram ID: {uploaded_by}",
        f"Строк в листе: {getattr(summary, 'total_rows', 0)}",
        f"Распознано матчей: {getattr(summary, 'parsed_matches', 0)}",
        f"Новых / обновлено: {getattr(summary, 'inserted_matches', 0)} / {getattr(summary, 'updated_matches', 0)}",
        f"Сигналов: {getattr(summary, 'scheduled_signals', 0)} (VIP: {groups.get('vip', 0)}, ALL: {groups.get('all', 0)})",
        f"Отменено сигналов: {getattr(summary, 'cancelled_signals', 0)}",
    ]
    if warnings:
        lines.append(f"Предупреждений: {len(warnings)}")
    lines.append("Проверьте детали в web-админке: Мониторинг или Сигналы.")
    return "\n".join(lines)[:3900]


def format_import_error_admin_text(file_name: str, uploaded_by: int, error: object) -> str:
    return "\n".join([
        "⚠️ Ошибка импорта Excel",
        "",
        f"Файл: {file_name}",
        f"Загрузил Telegram ID: {uploaded_by}",
        f"Ошибка: {str(error)[:900]}",
        "Проверьте файл и раздел Мониторинг в web-админке.",
    ])[:3900]


def format_delivery_failure_admin_text(processed_signals: int, failed_deliveries: int, sent_deliveries: int) -> str:
    return "\n".join([
        "⚠️ Ошибка доставки сигналов",
        "",
        f"Проверено сигналов: {processed_signals}",
        f"Успешных доставок: {sent_deliveries}",
        f"Ошибок доставки: {failed_deliveries}",
        "Проверьте web-админку: Доставки → Ошибка.",
    ])


def format_backup_error_admin_text(error: object) -> str:
    return "\n".join([
        "⚠️ Проблема с backup SQLite",
        "",
        f"Ошибка: {str(error)[:900]}",
        "Проверьте раздел Обслуживание в web-админке и логи контейнера.",
    ])[:3900]
