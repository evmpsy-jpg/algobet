from datetime import datetime

from app.database.models import ImportBatch, Match, ScheduledSignal
from app.handlers.user import format_tournament_analytics


def make_match() -> Match:
    return Match(
        external_match_id=1001,
        external_tournament_id=2001,
        source_url="https://example.test/tournaments/2001/1001",
        tournament_date="22.07.2026",
        match_time="12:00",
        match_start_at=datetime(2026, 7, 22, 12, 0),
        player_1="Игрок 1",
        player_2="Игрок 2",
        player_1_rating=None,
        player_2_rating=None,
        score=None,
        raw_data={},
        is_present_in_latest_import=True,
    )


def test_format_tournament_analytics_without_import() -> None:
    text = format_tournament_analytics(
        latest_import=None,
        total_matches=0,
        active_matches=0,
        scheduled_signals=0,
        ready_signals=0,
        upcoming_matches=[],
        upcoming_signals=[],
    )

    assert "Данные турниров пока не загружены" in text


def test_format_tournament_analytics_shows_matches_and_signals() -> None:
    batch = ImportBatch(
        file_name="sample.xlsx",
        stored_path="sample.xlsx",
        file_sha256="abc",
        uploaded_by_telegram_id=1,
        status="completed",
        parsed_matches=10,
        created_at=datetime(2026, 7, 22, 7, 0),
        finished_at=datetime(2026, 7, 22, 7, 1),
    )
    match = make_match()
    signal = ScheduledSignal(
        match_id=1,
        status="scheduled",
        send_at=datetime(2026, 7, 22, 7, 40),
        signal_payload={"level": "TOP", "side": 1},
        message_text="signal",
    )

    text = format_tournament_analytics(
        latest_import=batch,
        total_matches=10,
        active_matches=8,
        scheduled_signals=2,
        ready_signals=1,
        upcoming_matches=[match],
        upcoming_signals=[(signal, match)],
    )

    assert "Матчей в базе: 10" in text
    assert "Актуальных матчей: 8" in text
    assert "Запланированных сигналов: 2" in text
    assert "Готовых к отправке: 1" in text
    assert "22.07 15:00 · Игрок 1 — Игрок 2" in text
    assert "22.07 10:40 · TOP · П1" in text
