from __future__ import annotations

from app.domain.models import MatchData, Signal, SignalDecision
from app.services.rules_config import get_signal_rules


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
        selected_player=decision.selected_player,
        probability=decision.probability,
        h2h_p1=match.h2h_p1,
        h2h_p2=match.h2h_p2,
        average_h2h_handicap=match.average_h2h_handicap,
        average_difference=match.average_difference,
        h2h_games=match.h2h_games,
        recent_h2h_wins=payload.get("recent_h2h_wins"),
        recent_h2h_total=5,
        set1_handicap=match.set1_handicap,
        set2_handicap=match.set2_handicap,
        set3_handicap=match.set3_handicap,
        lead_minutes=int(cfg["signal"].get("lead_minutes", 10)),
    )
