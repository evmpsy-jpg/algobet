from __future__ import annotations

import re
from html import escape
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


def _player_with_rating(name: str, rating: int | None) -> str:
    return f"({rating}) {name}" if rating else name


def _html(value: Any) -> str:
    return escape(str(value or ""), quote=False)

def _match_link(player_1_line: str, player_2_line: str, source_url: str | None) -> str:
    label = f"{player_1_line} ⚔️ {player_2_line}"
    href = str(source_url or "").strip()
    if not href:
        return _html(label)
    return f"<a href=\"{escape(href, quote=True)}\">{_html(label)}</a>"

def _signal_group_level(signal_group: str | None) -> str:
    return "VIP" if str(signal_group or "").strip().lower() == "vip" else "STANDART"

def _advantage_text(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "⚪ Нет преимущества"
    if number > 0:
        return f"🟢 Преимущество П1 = {_fmt(abs(number), 0)} Бал."
    if number < 0:
        return f"🔴 Преимущество П2 = {_fmt(abs(number), 0)} Бал."
    return "⚪ Нет преимущества"


def format_signal(signal: Signal) -> str:
    path = Path("templates/signal.txt")
    if not path.exists():
        raise FileNotFoundError("Не найден шаблон templates/signal.txt")
    template = path.read_text(encoding="utf-8")
    signal_marker = "🔴" if signal.side == 2 else "🟢"
    values = {
        "title": _html(signal.title),
        "lead_minutes": signal.lead_minutes,
        "signal_group_level": _html(_signal_group_level(signal.signal_group)),
        "tournament_line": _html(_tournament_line(signal.tournament_name)),
        "match_time": _html(signal.match_time),
        "player_1": _html(signal.player_1),
        "player_2": _html(signal.player_2),
        "player_1_line": _html(_player_with_rating(signal.player_1, signal.player_1_rating)),
        "player_2_line": _html(_player_with_rating(signal.player_2, signal.player_2_rating)),
        "match_link": _match_link(
            _player_with_rating(signal.player_1, signal.player_1_rating),
            _player_with_rating(signal.player_2, signal.player_2_rating),
            signal.source_url,
        ),
        "signal_marker": signal_marker,
        "probability": _fmt(signal.probability, 0),
        "probability_p1": _fmt(signal.probability_p1, 0),
        "probability_p2": _fmt(signal.probability_p2, 0),
        "favorite_form": _fmt(signal.favorite_form, 0),
        "favorite_form_p1": _fmt(signal.favorite_form_p1, 0),
        "favorite_form_p2": _fmt(signal.favorite_form_p2, 0),
        "selected_player": _html(signal.selected_player),
        "p1_points": _fmt(signal.p1_points, 0),
        "p2_points": _fmt(signal.p2_points, 0),
        "advantage_text": _html(_advantage_text(signal.advantage)),
        "set1_handicap": _fmt(signal.set1_handicap, 1, True),
        "set2_handicap": _fmt(signal.set2_handicap, 1, True),
        "set3_handicap": _fmt(signal.set3_handicap, 1, True),
        "set4_handicap": _fmt(signal.set4_handicap, 1, True) if signal.set4_handicap is not None else "",
        "set5_handicap": _fmt(signal.set5_handicap, 1, True) if signal.set5_handicap is not None else "",
        "h2h_p1": _fmt(signal.h2h_p1, 0),
        "h2h_p2": _fmt(signal.h2h_p2, 0),
        "selected_h2h_wins": _fmt(signal.h2h_p1 if signal.side == 1 else signal.h2h_p2, 0),
        "opponent_h2h_wins": _fmt(signal.h2h_p2 if signal.side == 1 else signal.h2h_p1, 0),
        "average_h2h_handicap": _fmt(signal.average_h2h_handicap, 1, True),
        "average_difference": _fmt(signal.average_difference, 1, True),
        "h2h_games": _fmt(signal.h2h_games, 0),
        "recent_h2h_wins": _fmt(signal.recent_h2h_wins, 0),
        "recent_h2h_total": signal.recent_h2h_total,
    }
    return template.format_map(values).strip()
