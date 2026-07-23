from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class MatchData:
    match_id: int
    tournament_id: int
    source_url: str
    tournament_date: str
    tournament_name: str
    match_time: str
    match_start_at: datetime
    player_1: str
    player_2: str
    player_1_rating: int | None = None
    player_2_rating: int | None = None
    score: str | None = None

    h2h_games: float | None = None
    form_p1: float | None = None
    form_p2: float | None = None
    bg_p1: float | None = None
    bf_p2: float | None = None
    probability_p1: float | None = None
    probability_p2: float | None = None
    all_signal_p1: float | None = None
    all_signal_p2: float | None = None
    p1_exact: float | None = None
    p2_exact: float | None = None
    p1_range: float | None = None
    p2_range: float | None = None
    h2h_p1: float | None = None
    h2h_p2: float | None = None
    average_h2h_handicap: float | None = None
    average_difference: float | None = None
    set1_handicap: float | None = None
    set2_handicap: float | None = None
    set3_handicap: float | None = None

    raw_data: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RuleTrace:
    code: str
    label: str
    passed: bool
    actual: Any = None
    expected: Any = None
    side: int | None = None
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SignalDecision:
    suitable: bool
    side: int | None = None
    selected_player: str | None = None
    probability: float | None = None
    level: str | None = None
    signal_type: str | None = None
    reason: str | None = None
    traces: list[RuleTrace] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Signal:
    algorithm_version: str
    level: str
    title: str
    match_id: int
    tournament_name: str
    match_time: str
    player_1: str
    player_2: str
    selected_player: str
    probability: float | None
    h2h_p1: float | None
    h2h_p2: float | None
    average_h2h_handicap: float | None
    average_difference: float | None
    h2h_games: float | None
    recent_h2h_wins: float | None
    recent_h2h_total: int
    set1_handicap: float | None
    set2_handicap: float | None
    set3_handicap: float | None
    lead_minutes: int
