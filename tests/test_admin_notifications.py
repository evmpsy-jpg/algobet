from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.admin_notifications import (
    format_backup_error_admin_text,
    format_delivery_failure_admin_text,
    format_import_error_admin_text,
    format_import_success_admin_text,
    notify_admins,
)


class FakeBot:
    def __init__(self, *, fail_for: set[int] | None = None) -> None:
        self.fail_for = fail_for or set()
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, *, chat_id: int, text: str, **_: object) -> None:
        if chat_id in self.fail_for:
            raise RuntimeError("telegram unavailable")
        self.messages.append((chat_id, text))


@pytest.mark.asyncio
async def test_notify_admins_skips_duplicates_and_excluded_ids() -> None:
    bot = FakeBot()

    delivered = await notify_admins(bot, [1, 2, 2, 3], "test", exclude_ids=[2])

    assert delivered == 2
    assert bot.messages == [(1, "test"), (3, "test")]


@pytest.mark.asyncio
async def test_notify_admins_continues_after_failed_admin() -> None:
    bot = FakeBot(fail_for={2})

    delivered = await notify_admins(bot, [1, 2, 3], "test")

    assert delivered == 2
    assert bot.messages == [(1, "test"), (3, "test")]


def test_import_success_notification_text_contains_core_metrics() -> None:
    summary = SimpleNamespace(
        total_rows=100,
        parsed_matches=80,
        inserted_matches=10,
        updated_matches=70,
        scheduled_signals=5,
        cancelled_signals=2,
        scheduled_by_group={"vip": 2, "all": 3},
        warnings=["row warning"],
    )

    text = format_import_success_admin_text(summary, "ЛЕТО.xlsx", 315715137, ["#12: ожидалось 20 мин"])

    assert "Excel загружен" in text
    assert "ЛЕТО.xlsx" in text
    assert "Сигналов: 5 (VIP: 2, ALL: 3)" in text
    assert "Предупреждений: 1" in text
    assert "Проблемы расписания: 1" in text
    assert "#12: ожидалось 20 мин" in text


def test_error_notification_texts_are_russian_and_actionable() -> None:
    assert "Ошибка импорта Excel" in format_import_error_admin_text("bad.xlsx", 1, RuntimeError("boom"))
    assert "Ошибок доставки: 3" in format_delivery_failure_admin_text(4, 3, 1)
    assert "Проблема с backup SQLite" in format_backup_error_admin_text(FileNotFoundError("missing db"))
