from datetime import datetime

from app.database.models import Match, ScheduledSignal, SignalResult
from app.services.promo_publisher import format_promo_signal_post


def _match() -> Match:
    return Match(
        external_match_id=123,
        external_tournament_id=456,
        source_url="https://example.test/match/123",
        tournament_date="08.08.2026",
        match_time="20:00",
        match_start_at=datetime(2026, 8, 8, 17, 0),
        player_1="Попов О. А.",
        player_2="Борисов А. С.",
        player_1_rating=446,
        player_2_rating=468,
        score="1:3",
        raw_data={"_tournament_name": "Турнир А9. Лига 400-450"},
    )


def test_format_promo_signal_post_won_vip() -> None:
    signal = ScheduledSignal(
        match_id=1,
        status="sent",
        send_at=datetime(2026, 8, 8, 16, 50),
        signal_payload={"signal_group": "vip", "selected_player": "Борисов А. С.", "side": 2},
    )
    result = SignalResult(signal_id=1, status="won", source="auto")

    text = format_promo_signal_post(signal, _match(), result)

    assert "Уровень: VIP" in text
    assert "📌 Матч: (446) Попов О. А. ⚔️ (468) Борисов А. С." in text
    assert "👉 Выбор: Победа в сете - Борисов А. С." in text
    assert "Счет матча: 1:3" in text
    assert "🟢 ЗАШЕЛ" in text
    assert text.endswith("@algobett_bot")


def test_format_promo_signal_post_lost_standard() -> None:
    signal = ScheduledSignal(
        match_id=1,
        status="sent",
        send_at=datetime(2026, 8, 8, 16, 50),
        signal_payload={"signal_group": "all", "selected_player": "Попов О. А.", "side": 1},
    )
    result = SignalResult(signal_id=1, status="lost", source="auto")

    text = format_promo_signal_post(signal, _match(), result)

    assert "Уровень: STANDART" in text
    assert "🔴 НЕ ЗАШЕЛ" in text
