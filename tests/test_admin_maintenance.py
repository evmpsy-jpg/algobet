from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from app.handlers.admin import format_maintenance_text, format_uptime, maintenance_keyboard
from app.keyboards.common import admin_menu


def test_format_uptime_shows_days_hours_minutes() -> None:
    started = datetime(2026, 7, 24, 8, 0)
    now = started + timedelta(days=1, hours=2, minutes=3, seconds=4)

    assert format_uptime(started, now) == "1 д 2 ч 3 мин"


def test_maintenance_keyboard_has_backup_and_refresh_buttons() -> None:
    markup = maintenance_keyboard()

    callbacks = [row[0].callback_data for row in markup.inline_keyboard]

    assert callbacks == ["maint:backup", "maint:refresh"]


def test_format_maintenance_text_shows_backup_settings(tmp_path) -> None:
    settings = SimpleNamespace(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'algobet.db'}",
        data_dir=tmp_path / "data",
        uploads_dir=tmp_path / "uploads",
        sqlite_backup_enabled=True,
        sqlite_backup_interval_hours=24,
        sqlite_backup_keep=10,
    )

    text = format_maintenance_text(
        settings,
        started_at=datetime(2026, 7, 24, 8, 0),
        now=datetime(2026, 7, 24, 9, 5),
    )

    assert "Обслуживание" in text
    assert "Uptime: 1 ч 5 мин" in text
    assert "Auto backup: включен" in text
    assert "Интервал: 24 ч" in text
    assert "Хранить копий: 10" in text


def test_admin_menu_has_maintenance_button() -> None:
    texts = [button.text for row in admin_menu().keyboard for button in row]

    assert "🛠 Обслуживание" in texts
