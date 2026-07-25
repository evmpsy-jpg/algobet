from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models import Base, ImportBatch, Match, ScheduledSignal, SignalDecisionLog, SignalDelivery, SignalResult, User
from app.handlers.admin import admin_settings_keyboard, directory_size_bytes, format_bytes, format_latest_import_text, format_schedule_warning_lines, format_sent_history_summary, format_signals_dashboard_text, format_signal_deliveries, get_signal_group_counts, remove_uploaded_file, result_filter_keyboard, signal_list_keyboard, signals_dashboard_keyboard, sqlite_database_path, storage_usage_lines, summarize_import_decision_logs


def make_match() -> Match:
    return Match(
        external_match_id=1001,
        external_tournament_id=2001,
        source_url="https://example.test/tournaments/2001/1001",
        tournament_date="21.07.2026",
        match_time="12:00",
        match_start_at=datetime(2026, 7, 21, 12, 0),
        player_1="Игрок 1",
        player_2="Игрок 2",
        player_1_rating=None,
        player_2_rating=None,
        score=None,
        raw_data={},
    )


def test_format_signal_deliveries_shows_success_and_error() -> None:
    signal = ScheduledSignal(
        match_id=1,
        status="sent",
        send_at=datetime(2026, 7, 21, 11, 40),
        signal_payload={"signal_group": "all"},
        message_text="signal text",
    )
    user = User(
        telegram_id=111,
        username="tester",
        first_name="Тест",
        last_name="Игрок",
    )
    sent = SignalDelivery(
        signal_id=1,
        user_id=1,
        telegram_id=111,
        status="sent",
        sent_at=datetime(2026, 7, 21, 11, 40),
    )
    failed = SignalDelivery(
        signal_id=1,
        user_id=2,
        telegram_id=222,
        status="failed",
        error_text="telegram unavailable",
    )

    text = format_signal_deliveries(signal, make_match(), [(sent, user), (failed, None)])

    assert "Доставки сигнала" in text
    assert "Отправлено: 1" in text
    assert "Ошибок: 1" in text
    assert "Тест Игрок" in text
    assert "telegram unavailable" in text

def test_format_signal_deliveries_converts_utc_delivery_time_to_local_timezone() -> None:
    signal = ScheduledSignal(
        match_id=1,
        status="sent",
        send_at=datetime(2026, 7, 22, 7, 40),
        message_text="signal text",
    )
    user = User(
        telegram_id=111,
        username="tester",
        first_name="Евгений",
        last_name="Мельников",
    )
    delivery = SignalDelivery(
        signal_id=1,
        user_id=1,
        telegram_id=111,
        status="sent",
        sent_at=datetime(2026, 7, 22, 7, 53),
    )

    text = format_signal_deliveries(signal, make_match(), [(delivery, user)])

    assert "22.07 10:53" in text

def test_format_sent_history_summary_shows_recent_signal_results() -> None:
    signal = ScheduledSignal(
        id=10,
        match_id=1,
        status="sent",
        send_at=datetime(2026, 7, 22, 7, 40),
        sent_at=datetime(2026, 7, 22, 7, 53),
        signal_payload={"level": "TOP", "side": 1, "signal_group": "vip"},
        message_text="signal text",
    )
    result = SignalResult(signal_id=10, status="won", source="auto")

    text = format_sent_history_summary(
        sent=1,
        ready=2,
        delivered=3,
        failed=1,
        recent_rows=[(signal, make_match(), result)],
        delivery_counts={10: {"sent": 3, "failed": 1, "pending": 0}},
    )

    assert "История выдачи сигналов" in text
    assert "Отправленных сигналов: 1" in text
    assert "22.07 10:53" in text
    assert "VIP" in text
    assert "TOP" in text
    assert "доставки: ✅ 3 / ❌ 1 / ⏳ 0" in text

def test_result_filter_keyboard_links_to_signal_and_history() -> None:
    signal = ScheduledSignal(
        id=10,
        match_id=1,
        status="sent",
        send_at=datetime(2026, 7, 22, 7, 40),
        sent_at=datetime(2026, 7, 22, 7, 53),
        signal_payload={"level": "TOP", "side": 1, "signal_group": "vip"},
        message_text="signal text",
    )

    markup = result_filter_keyboard([(signal, make_match(), None)], "unrated", page=0, total=1)

    assert "VIP" in markup.inline_keyboard[0][0].text
    assert markup.inline_keyboard[0][0].callback_data == "sig:view:10:sent:0"
    assert markup.inline_keyboard[-1][0].callback_data == "sig:history"


def test_signal_list_keyboard_filters_by_signal_group() -> None:
    signal = ScheduledSignal(
        id=10,
        match_id=1,
        status="ready",
        send_at=datetime(2026, 7, 22, 7, 40),
        signal_payload={"level": "TOP", "side": 1, "signal_group": "vip"},
        message_text="signal text",
    )

    markup = signal_list_keyboard([(signal, make_match(), None)], "ready", page=1, total=20, group_filter="vip")

    filter_row = markup.inline_keyboard[0]
    assert [button.callback_data for button in filter_row] == [
        "sig:list:ready:0",
        "sig:list:ready:0:vip",
        "sig:list:ready:0:all",
    ]
    assert filter_row[1].text.startswith("* ")
    assert "VIP" in markup.inline_keyboard[1][0].text
    assert markup.inline_keyboard[2][0].callback_data == "sig:list:ready:0:vip"
    assert markup.inline_keyboard[2][1].callback_data == "sig:list:ready:2:vip"


@pytest.mark.asyncio
async def test_get_signal_group_counts_summarizes_payload_groups(monkeypatch) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        matches = [make_match(), make_match(), make_match()]
        for index, match in enumerate(matches, start=1):
            match.external_match_id = 1000 + index
        session.add_all(matches)
        await session.flush()
        session.add_all([
            ScheduledSignal(match_id=matches[0].id, status="ready", send_at=datetime(2026, 7, 22, 7, 40), signal_payload={"signal_group": "vip"}),
            ScheduledSignal(match_id=matches[1].id, status="ready", send_at=datetime(2026, 7, 22, 7, 41), signal_payload={"signal_group": "all"}),
            ScheduledSignal(match_id=matches[2].id, status="ready", send_at=datetime(2026, 7, 22, 7, 42), signal_payload={}),
        ])
        await session.commit()

    monkeypatch.setattr("app.handlers.admin.SessionFactory", factory)

    counts = await get_signal_group_counts()

    assert counts == {"vip": 1, "all": 1, "unknown": 1}
    await engine.dispose()


def test_format_schedule_warning_lines_shows_signal_timing() -> None:
    lines = format_schedule_warning_lines([
        SimpleNamespace(
            id=12,
            send_at=datetime(2026, 7, 24, 9, 40),
            match_start_at=datetime(2026, 7, 24, 10, 0),
            lead_minutes=20,
            player_1="Player One",
            player_2="Player Two",
            schedule_warning="ожидалось 30 мин",
        )
    ])

    assert lines == ["#12: 24.07.2026 12:40 → матч 24.07.2026 13:00 (20 мин), Player One - Player Two: ожидалось 30 мин"]


def test_remove_uploaded_file_deletes_file_and_ignores_missing(tmp_path) -> None:
    path = tmp_path / "upload.xlsx"
    path.write_bytes(b"excel")

    assert remove_uploaded_file(path) is True
    assert path.exists() is False
    assert remove_uploaded_file(path) is False


def test_signals_dashboard_keyboard_has_cancelled_button() -> None:
    markup = signals_dashboard_keyboard({"scheduled": 1, "ready": 2, "sent": 3, "cancelled": 4})

    callbacks = [row[0].callback_data for row in markup.inline_keyboard]
    texts = [row[0].text for row in markup.inline_keyboard]

    assert "sig:list:cancelled:0" in callbacks
    assert any("4" in text for text in texts if text)


def test_format_signals_dashboard_text_uses_real_line_breaks() -> None:
    text = format_signals_dashboard_text(
        {"scheduled": 0, "ready": 0, "sent": 1, "cancelled": 0},
        {"vip": 1, "all": 0, "unknown": 0},
    )

    assert "\\n" not in text
    assert "\n\n\u041f\u043e \u0442\u0438\u043f\u0430\u043c:\nVIP: 1" in text


def test_admin_settings_keyboard_has_backup_button() -> None:
    markup = admin_settings_keyboard()

    callbacks = [row[0].callback_data for row in markup.inline_keyboard]

    assert "admset:backup" in callbacks


def test_latest_import_format_shows_signal_groups_and_rejection_reasons() -> None:
    batch = ImportBatch(
        file_name="sample.xlsx",
        stored_path="uploads/sample.xlsx",
        file_sha256="abc",
        uploaded_by_telegram_id=1,
        status="completed",
        total_rows=100,
        parsed_matches=3,
        inserted_matches=2,
        updated_matches=1,
        missing_matches=0,
        created_at=datetime(2026, 7, 24, 8, 0),
        finished_at=datetime(2026, 7, 24, 8, 1),
    )
    logs = [
        SignalDecisionLog(match_id=1, import_batch_id=1, algorithm_version="v1", source="import", suitable=True, side=1, selected_player="A", probability=99, level="TOP", signal_type="SET_VIP_TOP", decision_payload={"signal_group": "vip"}),
        SignalDecisionLog(match_id=2, import_batch_id=1, algorithm_version="v1", source="import", suitable=True, side=2, selected_player="B", probability=95, level="STRONG", signal_type="SET_ALL_STRONG", decision_payload={"signal_group": "all"}),
        SignalDecisionLog(match_id=3, import_batch_id=1, algorithm_version="v1", source="import", suitable=False, reason="No rule", decision_payload={}),
    ]

    group_counts, rejection_reasons = summarize_import_decision_logs(logs)
    text = format_latest_import_text(batch, group_counts, rejection_reasons, ["Row 7: test"])

    assert "sample.xlsx" in text
    assert "VIP: 1" in text
    assert "ALL: 1" in text
    assert "No rule: 1" in text
    assert "\u041f\u0440\u0435\u0434\u0443\u043f\u0440\u0435\u0436\u0434\u0435\u043d\u0438\u044f (1)" in text


def test_storage_usage_helpers_format_sizes_and_sqlite_path(tmp_path) -> None:
    data_dir = tmp_path / "data"
    uploads_dir = tmp_path / "uploads"
    data_dir.mkdir()
    uploads_dir.mkdir()
    (data_dir / "algobet.db").write_bytes(b"a" * 2048)
    (uploads_dir / "old.xlsx").write_bytes(b"b" * 1024)

    settings = SimpleNamespace(
        database_url=f"sqlite+aiosqlite:///{data_dir / 'algobet.db'}",
        data_dir=data_dir,
        uploads_dir=uploads_dir,
    )

    assert format_bytes(0) == "0 B"
    assert format_bytes(1536) == "1.5 KB"
    assert sqlite_database_path(settings.database_url) == data_dir / "algobet.db"
    assert directory_size_bytes(uploads_dir) == 1024

    lines = storage_usage_lines(settings)

    assert lines[0] == "\u0414\u0438\u0441\u043a:"
    assert "data: 2.0 KB" in lines
    assert "uploads: 1.0 KB" in lines
    assert "SQLite: 2.0 KB" in lines


def test_storage_usage_reports_external_database(tmp_path) -> None:
    settings = SimpleNamespace(
        database_url="postgresql+asyncpg://user:pass@db/algobet",
        data_dir=tmp_path / "data",
        uploads_dir=tmp_path / "uploads",
    )

    lines = storage_usage_lines(settings)

    assert "SQLite: \u0432\u043d\u0435\u0448\u043d\u044f\u044f \u0411\u0414" in lines
