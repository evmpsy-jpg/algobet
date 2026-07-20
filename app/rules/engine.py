from __future__ import annotations

from typing import Any

from app.domain.models import MatchData, RuleTrace, SignalDecision
from app.services.rules_config import get_signal_rules


def _base_condition(
    exact: float | None,
    range_value: float | None,
    config: dict[str, Any],
) -> bool:
    allowed = {
        float(item)
        for item in config.get("range_values", [])
    }

    return (
        exact == float(config["exact_value"])
        or range_value in allowed
    )


def _trace(
    code: str,
    label: str,
    passed: bool,
    actual: Any,
    expected: Any,
    side: int | None = None,
) -> RuleTrace:
    mark = "✅" if passed else "❌"

    return RuleTrace(
        code=code,
        label=label,
        passed=passed,
        actual=actual,
        expected=expected,
        side=side,
        message=(
            f"{mark} {label}: {actual} "
            f"(требуется {expected})"
        ),
    )


def evaluate_match(match: MatchData) -> SignalDecision:
    config = get_signal_rules()
    rules = config["signal"]
    traces: list[RuleTrace] = []

    # ---------------------------------------------------------
    # Проверка минимального количества матчей H2H.
    #
    # Пока сохраняем текущее правило:
    # CP >= min_h2h_games — матч допускается.
    # Глобальный STOP для CP будет отдельным изменением.
    # ---------------------------------------------------------
    min_h2h = float(rules["min_h2h_games"])

    cp_ok = (
        match.h2h_games is not None
        and match.h2h_games >= min_h2h
    )

    traces.append(
        _trace(
            code="CP_MIN",
            label="Количество H2H",
            passed=cp_ok,
            actual=match.h2h_games,
            expected=f">= {min_h2h:g}",
        )
    )

    if not cp_ok:
        return SignalDecision(
            suitable=False,
            reason=traces[-1].message,
            traces=traces,
        )

    # ---------------------------------------------------------
    # Первичные условия кандидатов.
    #
    # P1: EG == 5 или CS входит в {8, 9, 10}
    # P2: EH == 5 или CT входит в {8, 9, 10}
    # ---------------------------------------------------------
    p1_base = _base_condition(
        match.p1_exact,
        match.p1_range,
        rules["p1"],
    )

    p2_base = _base_condition(
        match.p2_exact,
        match.p2_range,
        rules["p2"],
    )

    traces.append(
        _trace(
            code="P1_BASE",
            label="Первичное условие P1",
            passed=p1_base,
            actual=(
                f"EG={match.p1_exact}; "
                f"CS={match.p1_range}"
            ),
            expected="EG=5 или CS∈{8,9,10}",
            side=1,
        )
    )

    traces.append(
        _trace(
            code="P2_BASE",
            label="Первичное условие P2",
            passed=p2_base,
            actual=(
                f"EH={match.p2_exact}; "
                f"CT={match.p2_range}"
            ),
            expected="EH=5 или CT∈{8,9,10}",
            side=2,
        )
    )

    # ---------------------------------------------------------
    # Дополнительные фильтры.
    # ---------------------------------------------------------
    min_form = float(rules["min_favorite_form"])
    min_bg = float(rules["min_bg_p1"])
    min_bf = float(rules["min_bf_p2"])

    p1_form_ok = (
        match.form_p1 is not None
        and match.form_p1 >= min_form
    )

    p1_bg_ok = (
        match.bg_p1 is not None
        and match.bg_p1 >= min_bg
    )

    p2_form_ok = (
        match.form_p2 is not None
        and match.form_p2 >= min_form
    )

    p2_bf_ok = (
        match.bf_p2 is not None
        and match.bf_p2 >= min_bf
    )

    if p1_base:
        traces.append(
            _trace(
                code="P1_FORM",
                label="Форма P1 (Q)",
                passed=p1_form_ok,
                actual=match.form_p1,
                expected=f">= {min_form:g}",
                side=1,
            )
        )

        traces.append(
            _trace(
                code="P1_BG",
                label="Фильтр P1 (BG)",
                passed=p1_bg_ok,
                actual=match.bg_p1,
                expected=f">= {min_bg:g}",
                side=1,
            )
        )

    if p2_base:
        traces.append(
            _trace(
                code="P2_FORM",
                label="Форма P2 (X)",
                passed=p2_form_ok,
                actual=match.form_p2,
                expected=f">= {min_form:g}",
                side=2,
            )
        )

        traces.append(
            _trace(
                code="P2_BF",
                label="Фильтр P2 (BF)",
                passed=p2_bf_ok,
                actual=match.bf_p2,
                expected=f">= {min_bf:g}",
                side=2,
            )
        )

    p1_ok = (
        p1_base
        and p1_form_ok
        and p1_bg_ok
    )

    p2_ok = (
        p2_base
        and p2_form_ok
        and p2_bf_ok
    )

    if not p1_ok and not p2_ok:
        failed = [
            item.message
            for item in traces
            if not item.passed
        ]

        return SignalDecision(
            suitable=False,
            reason="; ".join(failed),
            traces=traces,
        )

    # ---------------------------------------------------------
    # Проверка вероятностей из Excel.
    #
    # Вероятность бот не рассчитывает.
    # Сторона может быть выбрана только при наличии вероятности.
    # ---------------------------------------------------------
    p1_probability_available = (
        p1_ok
        and match.probability_p1 is not None
    )

    p2_probability_available = (
        p2_ok
        and match.probability_p2 is not None
    )

    if p1_ok:
        traces.append(
            _trace(
                code="P1_PROBABILITY",
                label="Вероятность P1 (CV)",
                passed=match.probability_p1 is not None,
                actual=match.probability_p1,
                expected="значение из Excel",
                side=1,
            )
        )

    if p2_ok:
        traces.append(
            _trace(
                code="P2_PROBABILITY",
                label="Вероятность P2 (CW)",
                passed=match.probability_p2 is not None,
                actual=match.probability_p2,
                expected="значение из Excel",
                side=2,
            )
        )

    if (
        not p1_probability_available
        and not p2_probability_available
    ):
        return SignalDecision(
            suitable=False,
            reason=(
                "Нет вероятности из Excel ни для одной "
                "стороны, прошедшей правила"
            ),
            traces=traces,
        )

     # ---------------------------------------------------------
    # Выбор стороны.
    # ---------------------------------------------------------
    if (
        p1_probability_available
        and p2_probability_available
    ):
        if match.probability_p1 == match.probability_p2:
            traces.append(
                _trace(
                    code="PROBABILITY_TIE",
                    label="Сравнение вероятностей",
                    passed=False,
                    actual=(
                        f"CV={match.probability_p1}; "
                        f"CW={match.probability_p2}"
                    ),
                    expected="CV и CW должны различаться",
                )
            )

            return SignalDecision(
                suitable=False,
                reason=(
                    "Вероятности игроков равны: "
                    f"CV={match.probability_p1}, "
                    f"CW={match.probability_p2}. "
                    "Сигнал не формируется"
                ),
                traces=traces,
            )

        side = (
            1
            if match.probability_p1 > match.probability_p2
            else 2
        )

    elif p1_probability_available:
        side = 1

    else:
        side = 2

    probability = (
        match.probability_p1
        if side == 1
        else match.probability_p2
    )

    selected_player = (
        match.player_1
        if side == 1
        else match.player_2
    )

    recent_h2h = (
        match.p1_exact
        if side == 1
        else match.p2_exact
    )

    # ---------------------------------------------------------
    # Определение уровня сигнала.
    # ---------------------------------------------------------
    levels = config.get("levels", {})

    top_min = float(
        levels.get("top", {}).get(
            "min_probability",
            90,
        )
    )

    strong_min = float(
        levels.get("strong", {}).get(
            "min_probability",
            85,
        )
    )

    standard_min = float(
        levels.get("standard", {}).get(
            "min_probability",
            0,
        )
    )

    # Здесь probability уже гарантированно не None.
    numeric_probability = float(probability)

    if numeric_probability >= top_min:
        level = "TOP"
    elif numeric_probability >= strong_min:
        level = "STRONG"
    elif numeric_probability >= standard_min:
        level = "STANDARD"
    else:
        level = "STANDARD"

    level_cfg = levels.get(level.lower(), {})

    title = str(
        level_cfg.get(
            "title",
            "🎯 СИГНАЛ — НА СЕТ",
        )
    )

    traces.append(
        _trace(
            code="SIDE",
            label="Выбранная сторона",
            passed=True,
            actual=f"P{side} / {selected_player}",
            expected="лучшая прошедшая сторона",
            side=side,
        )
    )

    traces.append(
        _trace(
            code="LEVEL",
            label="Уровень сигнала",
            passed=True,
            actual=level,
            expected=f"вероятность {probability}",
            side=side,
        )
    )

    payload = {
        "algorithm_version": str(
            config.get("algorithm", {}).get(
                "version",
                "v1",
            )
        ),
        "side": side,
        "selected_player": selected_player,
        "confidence": probability,
        "probability": probability,
        "level": level,
        "title": title,
        "is_high_confidence": level == "TOP",
        "h2h_p1": match.h2h_p1,
        "h2h_p2": match.h2h_p2,
        "average_h2h_handicap": (
            match.average_h2h_handicap
        ),
        "average_difference": match.average_difference,
        "h2h_games": match.h2h_games,
        "recent_h2h_wins": recent_h2h,
        "recent_h2h_total": 5,
        "set1_handicap": match.set1_handicap,
        "set2_handicap": match.set2_handicap,
        "set3_handicap": match.set3_handicap,
        "decision_trace": [
            item.to_dict()
            for item in traces
        ],
    }

    return SignalDecision(
        suitable=True,
        side=side,
        selected_player=selected_player,
        probability=probability,
        level=level,
        signal_type=f"SET_{level}",
        traces=traces,
        payload=payload,
    )