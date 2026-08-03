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

    # ---------------------------------------------------------
    # Первичные условия кандидатов.
    #
    # Все сигналы:
    # P1: DG входит в {8, 9, 10} или EG = 4
    # P2: DH входит в {8, 9, 10} или EH = 4
    #
    # VIP сигналы:
    # P1: EG == 5 OR CS in {8, 9, 10}
    # P2: EH == 5 OR CT in {8, 9, 10}
    # ---------------------------------------------------------
    all_p1_allowed = {8.0, 9.0, 10.0}
    all_p2_allowed = {8.0, 9.0, 10.0}
    vip_range_allowed = {8.0, 9.0, 10.0}

    p1_all = match.all_signal_p1 in all_p1_allowed or match.p1_exact == 4
    p2_all = match.all_signal_p2 in all_p2_allowed or match.p2_exact == 4
    p1_vip = match.p1_exact == 5 or match.p1_range in vip_range_allowed
    p2_vip = match.p2_exact == 5 or match.p2_range in vip_range_allowed

    p1_base = p1_all or p1_vip
    p2_base = p2_all or p2_vip

    traces.append(
        _trace(
            code="P1_BASE",
            label="Первичное условие P1",
            passed=p1_base,
            actual=(
                f"DG={match.all_signal_p1}; "
                f"EG={match.p1_exact}; "
                f"CS={match.p1_range}"
            ),
            expected="ALL: DG in {8,9,10} or EG=4; VIP: EG=5 or CS in {8,9,10}",
            side=1,
        )
    )

    traces.append(
        _trace(
            code="P2_BASE",
            label="Первичное условие P2",
            passed=p2_base,
            actual=(
                f"DH={match.all_signal_p2}; "
                f"EH={match.p2_exact}; "
                f"CT={match.p2_range}"
            ),
            expected="ALL: DH in {8,9,10} or EH=4; VIP: EH=5 or CT in {8,9,10}",
            side=2,
        )
    )

    # ---------------------------------------------------------
    # Дополнительные фильтры.
    #
    # ALL проходит без STOP по CP и форме.
    # VIP требует CP >= min_h2h_games и форму выбранной стороны >= min_form.
    # Если сторона подходит и под ALL, и под VIP, оставляем её в ALL.
    # VIP используется только для чистых VIP-условий без пересечения с ALL.
    # ---------------------------------------------------------
    min_form = float(rules["min_favorite_form"])
    p1_form_ok = (
        match.form_p1 is not None
        and match.form_p1 >= min_form
    )

    p2_form_ok = (
        match.form_p2 is not None
        and match.form_p2 >= min_form
    )

    if p1_vip:
        traces.append(
            _trace(
                code="P1_FORM",
                label="Форма P1 (Q) для VIP",
                passed=p1_form_ok,
                actual=match.form_p1,
                expected=f">= {min_form:g}",
                side=1,
            )
        )

    if p2_vip:
        traces.append(
            _trace(
                code="P2_FORM",
                label="Форма P2 (X) для VIP",
                passed=p2_form_ok,
                actual=match.form_p2,
                expected=f">= {min_form:g}",
                side=2,
            )
        )

    p1_vip_ok = p1_vip and cp_ok and p1_form_ok
    p2_vip_ok = p2_vip and cp_ok and p2_form_ok
    p1_ok = (p1_all and cp_ok) or p1_vip_ok
    p2_ok = (p2_all and cp_ok) or p2_vip_ok
    p1_signal_group = "vip" if p1_vip_ok and not p1_all else "all" if p1_all or p1_vip_ok else None
    p2_signal_group = "vip" if p2_vip_ok and not p2_all else "all" if p2_all or p2_vip_ok else None

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


    favorite_form = (
        match.favorite_form_p1
        if side == 1
        else match.favorite_form_p2
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

    signal_group = (
        p1_signal_group
        if side == 1
        else p2_signal_group
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
        "favorite_form": favorite_form,
        "level": level,
        "signal_group": signal_group,
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
        "set4_handicap": match.set4_handicap,
        "set5_handicap": match.set5_handicap,
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
        signal_type=f"SET_{str(signal_group).upper()}_{level}",
        traces=traces,
        payload=payload,
    )
