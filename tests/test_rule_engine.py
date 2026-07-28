from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.domain.models import MatchData
from app.rules.engine import evaluate_match
from app.services.rules_config import get_signal_rules


@pytest.fixture(autouse=True)
def clear_rules_cache() -> None:
    get_signal_rules.cache_clear()
    yield
    get_signal_rules.cache_clear()


def make_match(**overrides: object) -> MatchData:
    values: dict[str, object] = {
        "match_id": 1001,
        "tournament_id": 2001,
        "source_url": "https://example.test/tournaments/2001/1001",
        "tournament_date": "19.07.2026",
        "tournament_name": "Тестовый турнир",
        "match_time": "20:00",
        "match_start_at": datetime(
            2026,
            7,
            19,
            20,
            0,
            tzinfo=ZoneInfo("Europe/Moscow"),
        ),
        "player_1": "Игрок 1",
        "player_2": "Игрок 2",
        "h2h_games": 5,
        "form_p1": 7,
        "form_p2": 7,
        "bg_p1": 1.3,
        "bf_p2": 1.3,
        "probability_p1": 88,
        "probability_p2": 86,
        "all_signal_p1": 8,
        "all_signal_p2": None,
        "p1_exact": 0,
        "p2_exact": 0,
        "p1_range": 0,
        "p2_range": 0,
        "h2h_p1": 3,
        "h2h_p2": 2,
        "average_h2h_handicap": 2.4,
        "average_difference": 4.8,
        "set1_handicap": -1.5,
        "set2_handicap": -2.0,
        "set3_handicap": -1.0,
    }
    values.update(overrides)
    return MatchData(**values)


def test_selects_p1_when_only_p1_qualifies() -> None:
    decision = evaluate_match(
        make_match(
            all_signal_p1=8,
            all_signal_p2=None,
            p1_exact=0,
            p1_range=0,
            p2_exact=0,
            p2_range=0,
        )
    )

    assert decision.suitable is True
    assert decision.side == 1
    assert decision.selected_player == "\u0418\u0433\u0440\u043e\u043a 1"
    assert decision.probability == 88
    assert decision.payload["signal_group"] == "all"
    assert decision.signal_type == "SET_ALL_STRONG"

def test_selects_p2_when_only_p2_qualifies() -> None:
    decision = evaluate_match(
        make_match(
            all_signal_p1=None,
            all_signal_p2=8,
            p1_exact=0,
            p1_range=0,
            p2_exact=0,
            p2_range=0,
            probability_p2=91,
        )
    )

    assert decision.suitable is True
    assert decision.side == 2
    assert decision.selected_player == "\u0418\u0433\u0440\u043e\u043a 2"
    assert decision.probability == 91
    assert decision.payload["signal_group"] == "all"
    assert decision.signal_type == "SET_ALL_TOP"

def test_selects_side_with_higher_probability_when_both_qualify() -> None:
    decision = evaluate_match(
        make_match(
            all_signal_p1=8,
            all_signal_p2=8,
            p1_exact=0,
            p2_exact=0,
            probability_p1=86,
            probability_p2=92,
        )
    )

    assert decision.suitable is True
    assert decision.side == 2
    assert decision.selected_player == "Игрок 2"
    assert decision.probability == 92


def test_rejects_when_probabilities_are_equal() -> None:
    decision = evaluate_match(
        make_match(
            all_signal_p1=8,
            all_signal_p2=8,
            p1_exact=0,
            p2_exact=0,
            probability_p1=90,
            probability_p2=90,
        )
    )

    assert decision.suitable is False
    assert decision.side is None
    assert decision.selected_player is None
    assert decision.probability is None
    assert "Вероятности игроков равны" in decision.reason

    tie_trace = next(
        trace
        for trace in decision.traces
        if trace.code == "PROBABILITY_TIE"
    )

    assert tie_trace.passed is False
    assert "CV=90" in str(tie_trace.actual)
    assert "CW=90" in str(tie_trace.actual)


def test_rejects_match_when_neither_base_condition_passes() -> None:
    decision = evaluate_match(
        make_match(
            all_signal_p1=None,
            all_signal_p2=None,
            p1_exact=3,
            p1_range=7,
            p2_exact=3,
            p2_range=7,
        )
    )

    assert decision.suitable is False
    assert decision.side is None


@pytest.mark.parametrize("range_value", [8, 9, 10])
def test_p1_range_values_qualify(range_value: int) -> None:
    decision = evaluate_match(
        make_match(
            all_signal_p1=None,
            p1_exact=5,
            p1_range=range_value,
            p2_exact=0,
            p2_range=0,
        )
    )

    assert decision.suitable is True
    assert decision.side == 1
    assert decision.payload["signal_group"] == "vip"


@pytest.mark.parametrize("range_value", [8, 9, 10])
def test_p2_range_values_qualify(range_value: int) -> None:
    decision = evaluate_match(
        make_match(
            all_signal_p1=None,
            all_signal_p2=None,
            p1_exact=0,
            p1_range=0,
            p2_exact=5,
            p2_range=range_value,
        )
    )

    assert decision.suitable is True
    assert decision.side == 2
    assert decision.payload["signal_group"] == "vip"


@pytest.mark.parametrize("all_value", [8, 9, 10])
def test_all_signal_p1_values_qualify(all_value: int) -> None:
    decision = evaluate_match(
        make_match(
            all_signal_p1=all_value,
            all_signal_p2=None,
            p1_exact=0,
            p1_range=0,
        )
    )

    assert decision.suitable is True
    assert decision.side == 1
    assert decision.payload["signal_group"] == "all"


@pytest.mark.parametrize("all_value", [8, 9, 10])
def test_all_signal_p2_values_qualify(all_value: int) -> None:
    decision = evaluate_match(
        make_match(
            all_signal_p1=None,
            all_signal_p2=all_value,
            p1_exact=0,
            p1_range=0,
            p2_exact=0,
            p2_range=0,
            probability_p2=91,
        )
    )

    assert decision.suitable is True
    assert decision.side == 2
    assert decision.payload["signal_group"] == "all"

def test_all_signal_p1_accepts_exact_four() -> None:
    decision = evaluate_match(
        make_match(
            all_signal_p1=None,
            all_signal_p2=None,
            p1_exact=4,
            p1_range=0,
        )
    )

    assert decision.suitable is True
    assert decision.side == 1
    assert decision.payload["signal_group"] == "all"


def test_all_signal_p2_accepts_exact_four() -> None:
    decision = evaluate_match(
        make_match(
            all_signal_p1=None,
            all_signal_p2=None,
            p1_exact=0,
            p1_range=0,
            p2_exact=4,
            p2_range=0,
            probability_p2=91,
        )
    )

    assert decision.suitable is True
    assert decision.side == 2
    assert decision.payload["signal_group"] == "all"


def test_negative_dh_no_longer_qualifies_all_signal_p2() -> None:
    decision = evaluate_match(
        make_match(
            all_signal_p1=None,
            all_signal_p2=-8,
            p1_exact=0,
            p1_range=0,
            p2_exact=0,
            p2_range=0,
            probability_p2=91,
        )
    )

    assert decision.suitable is False


def test_vip_p1_accepts_exact_or_range() -> None:
    exact_only = evaluate_match(make_match(all_signal_p1=None, p1_exact=5, p1_range=0))
    range_only = evaluate_match(make_match(all_signal_p1=None, p1_exact=0, p1_range=8))
    both = evaluate_match(make_match(all_signal_p1=None, p1_exact=5, p1_range=8))

    assert exact_only.suitable is True
    assert exact_only.payload["signal_group"] == "vip"
    assert range_only.suitable is True
    assert range_only.payload["signal_group"] == "vip"
    assert both.suitable is True
    assert both.payload["signal_group"] == "vip"


def test_vip_p2_accepts_exact_or_range() -> None:
    exact_only = evaluate_match(
        make_match(all_signal_p1=None, all_signal_p2=None, p2_exact=5, p2_range=0, probability_p2=91)
    )
    range_only = evaluate_match(
        make_match(all_signal_p1=None, all_signal_p2=None, p2_exact=0, p2_range=8, probability_p2=91)
    )
    both = evaluate_match(
        make_match(
            all_signal_p1=None,
            all_signal_p2=None,
            p2_exact=5,
            p2_range=8,
            probability_p2=91,
        )
    )

    assert exact_only.suitable is True
    assert exact_only.payload["signal_group"] == "vip"
    assert range_only.suitable is True
    assert range_only.payload["signal_group"] == "vip"
    assert both.suitable is True
    assert both.side == 2
    assert both.payload["signal_group"] == "vip"


def test_all_group_has_priority_when_all_and_vip_match() -> None:
    decision = evaluate_match(
        make_match(
            all_signal_p1=8,
            p1_exact=5,
            p1_range=8,
        )
    )

    assert decision.suitable is True
    assert decision.payload["signal_group"] == "all"


def test_boundary_values_are_accepted() -> None:
    decision = evaluate_match(
        make_match(
            p1_exact=5,
            form_p1=7,
            bg_p1=1.3,
        )
    )

    assert decision.suitable is True
    assert decision.side == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("form_p1", 6.99),
    ],
)
def test_p1_is_rejected_below_threshold(field: str, value: float) -> None:
    decision = evaluate_match(
        make_match(
            all_signal_p1=None,
            p1_exact=5,
            p2_exact=0,
            p2_range=0,
            **{field: value},
        )
    )

    assert decision.suitable is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("form_p2", 6.99),
    ],
)
def test_p2_is_rejected_below_threshold(field: str, value: float) -> None:
    decision = evaluate_match(
        make_match(
            all_signal_p1=None,
            all_signal_p2=None,
            p1_exact=0,
            p1_range=0,
            p2_exact=5,
            p2_range=8,
            **{field: value},
        )
    )

    assert decision.suitable is False


def test_bg_and_bf_do_not_block_old_signal_logic() -> None:
    p1 = evaluate_match(make_match(all_signal_p1=None, p1_exact=5, bg_p1=0))
    p2 = evaluate_match(
        make_match(
            all_signal_p1=None,
            all_signal_p2=None,
            p1_exact=0,
            p1_range=0,
            p2_exact=5,
            p2_range=0,
            bf_p2=0,
            probability_p2=91,
        )
    )

    assert p1.suitable is True
    assert p1.payload["signal_group"] == "vip"
    assert p2.suitable is True
    assert p2.payload["signal_group"] == "vip"


def test_set_handicaps_are_preserved_in_payload() -> None:
    decision = evaluate_match(
        make_match(
            set1_handicap=-1.5,
            set2_handicap=2.0,
            set3_handicap=0,
        )
    )

    assert decision.suitable is True
    assert decision.payload["set1_handicap"] == -1.5
    assert decision.payload["set2_handicap"] == 2.0
    assert decision.payload["set3_handicap"] == 0


def test_probability_levels() -> None:
    standard = evaluate_match(make_match(probability_p1=84))
    strong = evaluate_match(make_match(probability_p1=85))
    top = evaluate_match(make_match(probability_p1=90))

    assert standard.level == "STANDARD"
    assert strong.level == "STRONG"
    assert top.level == "TOP"


def test_all_signal_ignores_h2h_stop() -> None:
    decision = evaluate_match(make_match(h2h_games=4, all_signal_p1=8, p1_exact=0, p1_range=0))

    assert decision.suitable is True
    assert decision.payload["signal_group"] == "all"


def test_selects_p2_when_p1_probability_is_missing() -> None:
    decision = evaluate_match(
        make_match(
            all_signal_p1=8,
            all_signal_p2=8,
            p1_exact=0,
            p2_exact=0,
            probability_p1=None,
            probability_p2=91,
        )
    )

    assert decision.suitable is True
    assert decision.side == 2
    assert decision.selected_player == "\u0418\u0433\u0440\u043e\u043a 2"
    assert decision.probability == 91

def test_selects_p1_when_p2_probability_is_missing() -> None:
    decision = evaluate_match(
        make_match(
            all_signal_p1=8,
            all_signal_p2=8,
            p1_exact=0,
            p2_exact=0,
            probability_p1=89,
            probability_p2=None,
        )
    )

    assert decision.suitable is True
    assert decision.side == 1
    assert decision.selected_player == "\u0418\u0433\u0440\u043e\u043a 1"
    assert decision.probability == 89

def test_rejects_when_probabilities_are_missing() -> None:
    decision = evaluate_match(
        make_match(
            all_signal_p1=8,
            all_signal_p2=8,
            p1_exact=0,
            p2_exact=0,
            probability_p1=None,
            probability_p2=None,
        )
    )

    assert decision.suitable is False
    assert decision.side is None
    assert "\u041d\u0435\u0442 \u0432\u0435\u0440\u043e\u044f\u0442\u043d\u043e\u0441\u0442\u0438" in decision.reason

def test_accepts_exactly_five_h2h_games() -> None:
    decision = evaluate_match(
        make_match(
            h2h_games=5,
            all_signal_p1=8,
            p1_exact=0,
            p2_exact=0,
            p2_range=0,
        )
    )

    assert decision.suitable is True
    assert decision.side == 1

def test_accepts_more_than_five_h2h_games() -> None:
    decision = evaluate_match(
        make_match(
            h2h_games=12,
            all_signal_p1=8,
            p1_exact=0,
            p2_exact=0,
            p2_range=0,
        )
    )

    assert decision.suitable is True
    assert decision.side == 1


def test_vip_signal_still_requires_h2h_minimum() -> None:
    decision = evaluate_match(
        make_match(
            h2h_games=4,
            all_signal_p1=None,
            p1_exact=5,
            p1_range=0,
        )
    )

    assert decision.suitable is False


def test_all_signal_ignores_favorite_form_stop() -> None:
    decision = evaluate_match(
        make_match(
            form_p1=3,
            all_signal_p1=8,
            p1_exact=0,
            p1_range=0,
        )
    )

    assert decision.suitable is True
    assert decision.payload["signal_group"] == "all"


def test_vip_signal_still_requires_favorite_form() -> None:
    decision = evaluate_match(
        make_match(
            form_p1=3,
            all_signal_p1=None,
            p1_exact=5,
            p1_range=0,
        )
    )

    assert decision.suitable is False
