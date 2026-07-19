from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.domain.models import Signal


def _fmt(value: Any, digits: int = 1, signed: bool = False) -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number == 0:
        return "0"
    if number.is_integer():
        return f"{number:+.0f}" if signed else f"{number:.0f}"
    return f"{number:+.{digits}f}" if signed else f"{number:.{digits}f}"


def _tournament_line(name: str) -> str:
    cleaned = (name or "Турнир").strip()
    parts = re.split(r"\.\s*", cleaned, maxsplit=1)
    return f"{parts[0]} • {parts[1]}" if len(parts) > 1 and parts[1] else parts[0]


def format_signal(signal: Signal) -> str:
    path = Path("templates/signal.txt")
    if not path.exists():
        raise FileNotFoundError("Не найден шаблон templates/signal.txt")
    template = path.read_text(encoding="utf-8")
    values = {
        "title": signal.title,
        "lead_minutes": signal.lead_minutes,
        "tournament_line": _tournament_line(signal.tournament_name),
        "match_time": signal.match_time,
        "player_1": signal.player_1,
        "player_2": signal.player_2,
        "probability": _fmt(signal.probability, 0),
        "selected_player": signal.selected_player,
        "set1_handicap": _fmt(signal.set1_handicap, 1, True),
        "set2_handicap": _fmt(signal.set2_handicap, 1, True),
        "set3_handicap": _fmt(signal.set3_handicap, 1, True),
        "h2h_p1": _fmt(signal.h2h_p1, 0),
        "h2h_p2": _fmt(signal.h2h_p2, 0),
        "average_h2h_handicap": _fmt(signal.average_h2h_handicap, 1, True),
        "average_difference": _fmt(signal.average_difference, 1, True),
        "h2h_games": _fmt(signal.h2h_games, 0),
        "recent_h2h_wins": _fmt(signal.recent_h2h_wins, 0),
        "recent_h2h_total": signal.recent_h2h_total,
    }
    return template.format_map(values).strip()
