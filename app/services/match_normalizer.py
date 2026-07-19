from __future__ import annotations

from typing import Any

from app.config.excel_mapping import get_excel_mapping
from app.domain.models import MatchData
from app.services.excel_parser import ParsedMatch


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace("%", "").replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def normalize_match(parsed: ParsedMatch) -> MatchData:
    columns = get_excel_mapping()

    def get(field: str) -> float | None:
        return _number(parsed.raw_data.get(columns[field]))

    return MatchData(
        match_id=parsed.external_match_id,
        tournament_id=parsed.external_tournament_id,
        source_url=parsed.source_url,
        tournament_date=parsed.tournament_date,
        tournament_name=parsed.tournament_name,
        match_time=parsed.match_time,
        match_start_at=parsed.match_start_at,
        player_1=parsed.player_1,
        player_2=parsed.player_2,
        player_1_rating=parsed.player_1_rating,
        player_2_rating=parsed.player_2_rating,
        score=parsed.score,
        h2h_games=get("h2h_games"),
        form_p1=get("form_p1"),
        form_p2=get("form_p2"),
        bg_p1=get("bg_p1"),
        bf_p2=get("bf_p2"),
        probability_p1=get("probability_p1"),
        probability_p2=get("probability_p2"),
        p1_exact=get("p1_exact"),
        p2_exact=get("p2_exact"),
        p1_range=get("p1_range"),
        p2_range=get("p2_range"),
        h2h_p1=get("h2h_p1"),
        h2h_p2=get("h2h_p2"),
        average_h2h_handicap=get("average_h2h_handicap"),
        average_difference=get("average_difference"),
        set1_handicap=get("set1_handicap"),
        set2_handicap=get("set2_handicap"),
        set3_handicap=get("set3_handicap"),
        raw_data=parsed.raw_data,
    )
