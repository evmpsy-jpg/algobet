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
        raw_data={"CP": 5},
    )


def test_format_public_results_shows_recent_results_without_admin_source() -> None:
    hidden_signal = ScheduledSignal(
        id=9,
        match_id=1,
        status="sent",
        send_at=datetime(2026, 7, 22, 7, 35),
        sent_at=datetime(2026, 7, 22, 7, 50),
        signal_payload={"level": "STANDARD", "side": 2},
        message_text="hidden signal text",
    )
    hidden_match = make_match()
    hidden_match.raw_data = {"CP": 4}
    hidden_result = SignalResult(signal_id=9, status="lost", source="auto")

    visible_signal = ScheduledSignal(
        id=10,
        match_id=2,
        status="sent",
        send_at=datetime(2026, 7, 22, 7, 40),
        sent_at=datetime(2026, 7, 22, 7, 53),
        signal_payload={"level": "TOP", "side": 1, "signal_group": "vip"},
        message_text="signal text",
    )
    visible_match = make_match()
    visible_result = SignalResult(signal_id=10, status="won", source="auto")

    text = format_public_results(
        [(hidden_signal, hidden_match, hidden_result), (visible_signal, visible_match, visible_result)],
        total_sent=2,
    )

    assert "🏆 Результаты сигналов" in text
    assert "Оценено: 1 из 1" in text
    assert "Процент захода: 100.0%" in text
    assert "22.07 10:53" in text
    assert "✅ · TOP · П1" in text
    assert "hidden signal text" not in text
    assert "счёт: 3:1" in text
    assert "авто" not in text
    assert "вручную" not in text


def test_format_public_results_shows_vip_and_standart_blocks() -> None:
    vip_signal = ScheduledSignal(
        id=11,
        match_id=1,
        status="sent",
        send_at=datetime(2026, 7, 22, 7, 40),
        sent_at=datetime(2026, 7, 22, 7, 53),
        signal_payload={"level": "TOP", "side": 1, "signal_group": "vip"},
        message_text="vip signal",
    )
    standart_signal = ScheduledSignal(
        id=12,
        match_id=2,
        status="sent",
        send_at=datetime(2026, 7, 22, 8, 40),
        sent_at=datetime(2026, 7, 22, 8, 53),
        signal_payload={"level": "STANDARD", "side": 2, "signal_group": "all"},
        message_text="standard signal",
    )
    vip_match = make_match()
    standart_match = make_match()
    standart_match.score = "3:0"

    text = format_public_results(
        [
            (vip_signal, vip_match, SignalResult(signal_id=11, status="won", source="auto")),
            (standart_signal, standart_match, SignalResult(signal_id=12, status="lost", source="auto")),
        ],
        total_sent=2,
    )

    assert "VIP\nОценено: 1 из 1\n✅ Зашло: 1\n❌ Не зашло: 0" in text
    assert "STANDART\nОценено: 1 из 1\n✅ Зашло: 0\n❌ Не зашло: 1" in text
    assert text.index("VIP") < text.index("STANDART") < text.index("Последние результаты:")


def test_format_public_results_handles_empty_history() -> None:
    text = format_public_results([], total_sent=0)

    assert "пока нет оцененных сигналов" in text
    assert "Процент захода: —" in text
