from __future__ import annotations

from typing import Any

from app.domain.models import MatchData, RuleTrace, SignalDecision
from app.services.rules_config import get_signal_rules


def _base_condition(exact: float | None, range_value: float | None, config: dict[str, Any]) -> bool:
    allowed = {float(item) for item in config.get("range_values", [])}
    return exact == float(config["exact_value"]) or range_value in allowed


def _trace(code: str, label: str, passed: bool, actual: Any, expected: Any, side: int | None = None) -> RuleTrace:
    mark = "✅" if passed else "❌"
    return RuleTrace(
        code=code,
        label=label,
        passed=passed,
        actual=actual,
        expected=expected,
        side=side,
        message=f"{mark} {label}: {actual} (требуется {expected})",
    )


def evaluate_match(match: MatchData) -> SignalDecision:
    config = get_signal_rules()
    rules = config["signal"]
    traces: list[RuleTrace] = []

    min_h2h = float(rules["min_h2h_games"])
    cp_ok = match.h2h_games is not None and match.h2h_games >= min_h2h
    traces.append(_trace("CP_MIN", "Количество H2H", cp_ok, match.h2h_games, f">= {min_h2h:g}"))
    if not cp_ok:
        return SignalDecision(False, reason=traces[-1].message, traces=traces)

    p1_base = _base_condition(match.p1_exact, match.p1_range, rules["p1"])
    p2_base = _base_condition(match.p2_exact, match.p2_range, rules["p2"])
    traces.append(_trace("P1_BASE", "Первичное условие П1", p1_base, f"EG={match.p1_exact}; CS={match.p1_range}", "EG=5 или CS∈{8,9,10}", 1))
    traces.append(_trace("P2_BASE", "Первичное условие П2", p2_base, f"EH={match.p2_exact}; CT={match.p2_range}", "EH=5 или CT∈{8,9,10}", 2))

    min_form = float(rules["min_favorite_form"])
    min_bg = float(rules["min_bg_p1"])
    min_bf = float(rules["min_bf_p2"])

    p1_form_ok = match.form_p1 is not None and match.form_p1 >= min_form
    p1_bg_ok = match.bg_p1 is not None and match.bg_p1 >= min_bg
    p2_form_ok = match.form_p2 is not None and match.form_p2 >= min_form
    p2_bf_ok = match.bf_p2 is not None and match.bf_p2 >= min_bf

    if p1_base:
        traces.append(_trace("P1_FORM", "Форма П1 (Q)", p1_form_ok, match.form_p1, f">= {min_form:g}", 1))
        traces.append(_trace("P1_BG", "Фильтр П1 (BG)", p1_bg_ok, match.bg_p1, f">= {min_bg:g}", 1))
    if p2_base:
        traces.append(_trace("P2_FORM", "Форма П2 (X)", p2_form_ok, match.form_p2, f">= {min_form:g}", 2))
        traces.append(_trace("P2_BF", "Фильтр П2 (BF)", p2_bf_ok, match.bf_p2, f">= {min_bf:g}", 2))

    p1_ok = p1_base and p1_form_ok and p1_bg_ok
    p2_ok = p2_base and p2_form_ok and p2_bf_ok
    if not p1_ok and not p2_ok:
        failed = [item.message for item in traces if not item.passed]
        return SignalDecision(False, reason="; ".join(failed), traces=traces)

    if p1_ok and p2_ok:
        p1_rank = match.probability_p1 if match.probability_p1 is not None else -1
        p2_rank = match.probability_p2 if match.probability_p2 is not None else -1
        side = 1 if p1_rank >= p2_rank else 2
    else:
        side = 1 if p1_ok else 2

    probability = match.probability_p1 if side == 1 else match.probability_p2
    selected_player = match.player_1 if side == 1 else match.player_2
    recent_h2h = match.p1_exact if side == 1 else match.p2_exact

    levels = config.get("levels", {})
    top_min = float(levels.get("top", {}).get("min_probability", 90))
    strong_min = float(levels.get("strong", {}).get("min_probability", 85))
    standard_min = float(levels.get("standard", {}).get("min_probability", 0))
    numeric_probability = probability if probability is not None else -1
    if numeric_probability >= top_min:
        level = "TOP"
    elif numeric_probability >= strong_min:
        level = "STRONG"
    elif numeric_probability >= standard_min:
        level = "STANDARD"
    else:
        level = "STANDARD"

    level_cfg = levels.get(level.lower(), {})
    title = str(level_cfg.get("title", "🎯 СИГНАЛ — НА СЕТ"))
    traces.append(_trace("SIDE", "Выбранная сторона", True, f"П{side} / {selected_player}", "лучшая прошедшая сторона"))
    traces.append(_trace("LEVEL", "Уровень сигнала", True, level, f"вероятность {probability}"))

    payload = {
        "algorithm_version": str(config.get("algorithm", {}).get("version", "v1")),
        "side": side,
        "selected_player": selected_player,
        "confidence": probability,
        "probability": probability,
        "level": level,
        "title": title,
        "is_high_confidence": level == "TOP",
        "h2h_p1": match.h2h_p1,
        "h2h_p2": match.h2h_p2,
        "average_h2h_handicap": match.average_h2h_handicap,
        "average_difference": match.average_difference,
        "h2h_games": match.h2h_games,
        "recent_h2h_wins": recent_h2h,
        "recent_h2h_total": 5,
        "set1_handicap": match.set1_handicap,
        "set2_handicap": match.set2_handicap,
        "set3_handicap": match.set3_handicap,
        "decision_trace": [item.to_dict() for item in traces],
    }
    return SignalDecision(
        True,
        side=side,
        selected_player=selected_player,
        probability=probability,
        level=level,
        signal_type=f"SET_{level}",
        traces=traces,
        payload=payload,
    )
