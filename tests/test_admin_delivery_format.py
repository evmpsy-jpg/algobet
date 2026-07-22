from __future__ import annotations

from datetime import datetime

from app.database.models import Match, ScheduledSignal, SignalDelivery, User
from app.handlers.admin import format_signal_deliveries


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
