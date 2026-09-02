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
        total_sent=1,
    )

    assert "🏆 Результаты сигналов" in text
    assert "Оценено: 1 из 1" in text
    assert "Процент захода: 100.0%" in text
    assert "22.07 10:53" in text
    assert "✅ · VIP · П1" in text
    assert "TOP" not in text
    assert "hidden signal text" not in text
    assert "счёт: 3:1" in text
    assert "авто" not in text
    assert "вручную" not in text


def test_format_public_results_shows_vip_and_standard_blocks() -> None:
    vip_signal = ScheduledSignal(
        id=11,
        match_id=1,
        status="sent",
        send_at=datetime(2026, 7, 22, 7, 40),
        sent_at=datetime(2026, 7, 22, 7, 53),
        signal_payload={"level": "TOP", "side": 1, "signal_group": "vip"},
        message_text="vip signal",
    )
    standard_signal = ScheduledSignal(
        id=12,
        match_id=2,
        status="sent",
        send_at=datetime(2026, 7, 22, 8, 40),
        sent_at=datetime(2026, 7, 22, 8, 53),
        signal_payload={"level": "STANDARD", "side": 2, "signal_group": "all"},
        message_text="standard signal",
    )
    vip_match = make_match()
    standard_match = make_match()
    standard_match.score = "3:0"

    text = format_public_results(
        [
            (vip_signal, vip_match, SignalResult(signal_id=11, status="won", source="auto")),
            (standard_signal, standard_match, SignalResult(signal_id=12, status="lost", source="auto")),
        ],
        total_sent=2,
    )

    assert "VIP\nОценено: 1 из 1\n✅ Зашло: 1\n❌ Не зашло: 0" in text
    assert "STANDARD\nОценено: 1 из 1\n✅ Зашло: 0\n❌ Не зашло: 1" in text
    assert text.index("VIP") < text.index("STANDARD") < text.index("Последние результаты:")


def test_format_public_results_handles_empty_history() -> None:
    text = format_public_results([], total_sent=0)

    assert "пока нет оцененных сигналов" in text
    assert "Процент захода: —" in text

def test_format_public_results_keeps_historically_suitable_a15_result() -> None:
    signal = ScheduledSignal(
        id=13,
        match_id=3,
        status="sent",
        send_at=datetime(2026, 8, 31, 13, 20),
        sent_at=datetime(2026, 8, 31, 13, 20),
        signal_payload={"level": "STANDARD", "side": 2, "signal_group": "all"},
        message_text="historical A15 signal",
    )
    match = make_match()
    match.match_start_at = datetime(2026, 8, 31, 13, 30)
    match.match_time = "16:30"
    match.player_1 = "Девятников Д. Н."
    match.player_2 = "Король Д. И."
    match.score = "0:3"
    match.raw_data = {
        "_tournament_name": "Турнир A15. Лига 600-700",
        "CP": 42,
        "Q": 7,
        "X": 7,
        "CV": 20,
        "CW": 80,
        "DG": 8,
    }
    result = SignalResult(signal_id=13, status="lost", source="auto")

    text = format_public_results([(signal, match, result, True)], total_sent=1)

    assert "Оценено: 1 из 1" in text
    assert "❌ Не зашло: 1" in text
    assert "Девятников Д. Н. — Король Д. И." in text

def test_format_public_results_excludes_cancelled_signals() -> None:
    cancelled_signal = ScheduledSignal(
        id=14,
        match_id=4,
        status="cancelled",
        send_at=datetime(2026, 8, 31, 13, 20),
        sent_at=datetime(2026, 8, 31, 13, 20),
        signal_payload={"level": "STANDARD", "side": 2, "signal_group": "all"},
        message_text="cancelled signal",
    )
    match = make_match()
    match.raw_data = {"CP": 42}
    result = SignalResult(signal_id=14, status="lost", source="auto")

    text = format_public_results([(cancelled_signal, match, result, True)], total_sent=0)

    assert "Оценено: 0 из 0" in text
    assert "❌ Не зашло: 0" in text
    assert "cancelled signal" not in text

def test_format_public_results_shows_sent_total_with_unrated_signal() -> None:
    signal = ScheduledSignal(
        id=15,
        match_id=5,
        status="sent",
        send_at=datetime(2026, 9, 2, 9, 50),
        sent_at=datetime(2026, 9, 2, 9, 50),
        signal_payload={"level": "STANDARD", "side": 1, "signal_group": "all"},
        message_text="sent result",
    )
    match = make_match()
    result = SignalResult(signal_id=15, status="won", source="auto")

    text = format_public_results([(signal, match, result, True)], total_sent=2)

    assert "Оценено: 1 из 2" in text
    assert "✅ Зашло: 1" in text
