from datetime import datetime

from app.domain.models import MatchData
from app.rules.engine import evaluate_match
from app.signals.builder import build_signal
from app.signals.formatter import format_signal


def test_stage5_signal_contains_set_handicaps():
    match = MatchData(
        match_id=1,
        tournament_id=2,
        source_url="https://example.test",
        tournament_date="19.07.2026",
        tournament_name="Турнир A5. Лига 600-700",
        match_time="13:30",
        match_start_at=datetime(2026, 7, 19, 13, 30),
        player_1="Игрок 1",
        player_2="Игрок 2",
        h2h_games=10,
        form_p1=8,
        favorite_form_p1=89,
        bg_p1=1.5,
        probability_p1=92,
        all_signal_p1=8,
        p1_exact=0,
        h2h_p1=7,
        h2h_p2=3,
        average_h2h_handicap=-5.7,
        average_difference=3.6,
        set1_handicap=1.8,
        set2_handicap=-2.4,
        set3_handicap=0,
    )
    decision = evaluate_match(match)
    assert decision.suitable
    assert decision.level == "TOP"
    assert decision.payload["favorite_form"] == 89
    text = format_signal(build_signal(match, decision))
    assert "Уровень: STANDART" in text
    assert "<b>Текущая форма игроков:</b> 89%" in text
    assert "👉 <b>Выбор:</b> Победа в сете - <b>Игрок 1</b>" in text
    assert "1️⃣ Сет: +1.8" in text
    assert "2️⃣ Сет: -2.4" in text or "2️⃣ Сет: −2.4" in text
    assert "3️⃣ Сет: 0" in text
    assert "🏓 <b>Фора по мячам (Последние 5 H2H по сетам):</b>" in text

def test_stage5_signal_uses_red_marker_for_second_player():
    match = MatchData(
        match_id=1,
        tournament_id=2,
        source_url="https://example.test",
        tournament_date="19.07.2026",
        tournament_name="Турнир A5. Лига 600-700",
        match_time="13:30",
        match_start_at=datetime(2026, 7, 19, 13, 30),
        player_1="Игрок 1",
        player_2="Игрок 2",
        h2h_games=10,
        form_p2=8,
        favorite_form_p2=60,
        probability_p2=62,
        all_signal_p2=8,
        p2_exact=0,
    )

    decision = evaluate_match(match)
    assert decision.suitable
    assert decision.side == 2
    text = format_signal(build_signal(match, decision))
    assert "Уровень: STANDART" in text
    assert "<b>Математическая вероятность исхода:</b> —% ⚔️ 62%" in text
    assert "<b>Текущая форма игроков:</b> —% ⚔️ 60%" in text
    assert "👉 <b>Выбор:</b> Победа в сете - <b>Игрок 2</b>" in text

def test_stage5_signal_header_shows_vip_group():
    match = MatchData(
        match_id=1,
        tournament_id=2,
        source_url="https://example.test",
        tournament_date="19.07.2026",
        tournament_name="Турнир A5. Лига 600-700",
        match_time="13:30",
        match_start_at=datetime(2026, 7, 19, 13, 30),
        player_1="Игрок 1",
        player_2="Игрок 2",
        h2h_games=10,
        form_p1=8,
        favorite_form_p1=89,
        bg_p1=1.5,
        probability_p1=92,
        p1_exact=5,
    )

    decision = evaluate_match(match)
    assert decision.suitable
    decision.payload["signal_group"] = "vip"
    text = format_signal(build_signal(match, decision))
    assert decision.payload["signal_group"] == "vip"
    assert "Уровень: VIP" in text
