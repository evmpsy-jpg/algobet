from __future__ import annotations

from app.domain.models import MatchData, Signal, SignalDecision
from app.services.rules_config import get_signal_rules


def _relative_to_side(value: float | None, side: int | None) -> float | None:
    if value is None:
        return None
    return value if side == 1 else -value


def build_signal(match: MatchData, decision: SignalDecision) -> Signal:
    if not decision.suitable or not decision.selected_player or not decision.level:
        raise ValueError("Нельзя сформировать Signal из отклонённого решения")
    cfg = get_signal_rules()
    payload = decision.payload
    level_cfg = cfg.get("levels", {}).get(decision.level.lower(), {})
    return Signal(
        algorithm_version=str(payload.get("algorithm_version", "v1")),
        level=decision.level,
        title=str(payload.get("title") or level_cfg.get("title") or "🎯 СИГНАЛ — НА СЕТ"),
        match_id=match.match_id,
        tournament_name=match.tournament_name,
        match_time=match.match_time,
        player_1=match.player_1,
        player_2=match.player_2,
        player_1_rating=match.player_1_rating,
        player_2_rating=match.player_2_rating,
        probability_p1=match.probability_p1,
        probability_p2=match.probability_p2,
        favorite_form_p1=match.favorite_form_p1,
        favorite_form_p2=match.favorite_form_p2,
        selected_player=decision.selected_player,
        side=int(decision.side or 0),
        probability=decision.probability,
        favorite_form=payload.get("favorite_form"),
        h2h_p1=match.h2h_p1,
        h2h_p2=match.h2h_p2,
        average_h2h_handicap=_relative_to_side(match.average_h2h_handicap, decision.side),
        average_difference=match.average_difference,
        h2h_games=match.h2h_games,
        recent_h2h_wins=payload.get("recent_h2h_wins"),
        recent_h2h_total=5,
        set1_handicap=match.set1_handicap,
        set2_handicap=match.set2_handicap,
        set3_handicap=match.set3_handicap,
        set4_handicap=match.set4_handicap,
        set5_handicap=match.set5_handicap,
        lead_minutes=int(cfg["signal"].get("lead_minutes", 10)),
    )
