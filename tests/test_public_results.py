from datetime import datetime

from app.database.models import Match, ScheduledSignal, SignalResult
from app.handlers.user import format_public_results


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
        score="3:1",
        raw_data={},
    )


def test_format_public_results_shows_recent_results_without_admin_source() -> None:
    signal = ScheduledSignal(
        id=10,
        match_id=1,
        status="sent",
        send_at=datetime(2026, 7, 22, 7, 40),
        sent_at=datetime(2026, 7, 22, 7, 53),
        signal_payload={"level": "TOP", "side": 1},
        message_text="signal text",
    )
    result = SignalResult(signal_id=10, status="won", source="auto")

    text = format_public_results([(signal, make_match(), result)], total_sent=2)

    assert "🏆 Результаты сигналов" in text
    assert "Оценено: 1 из 2" in text
    assert "Процент захода: 100.0%" in text
    assert "22.07 10:53" in text
    assert "✅ · TOP · П1" in text
    assert "счёт: 3:1" in text
    assert "авто" not in text
    assert "вручную" not in text


def test_format_public_results_handles_empty_history() -> None:
    text = format_public_results([], total_sent=0)

    assert "пока нет оцененных сигналов" in text
    assert "Процент захода: —" in text
