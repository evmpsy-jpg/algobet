from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

DEFAULT_RULES: dict[str, Any] = {
    "algorithm": {"version": "v1.0"},
    "signal": {
        "lead_minutes": 10,
        "min_h2h_games": 5,
        "min_favorite_form": 7,
        "min_standard_form": 6,
        "min_extended_form": 5,
        "min_extended_probability": 65,
        "min_extended_average_difference": 2.2,
        "min_bg_p1": 1.3,
        "min_bf_p2": 1.3,
        "p1": {"exact_value": 5, "range_values": [8, 9, 10]},
        "p2": {"exact_value": 5, "range_values": [8, 9, 10]},
    },
    "levels": {
        "top": {"min_probability": 90, "title": "🔥 ТОП СИГНАЛ — НА СЕТ"},
        "strong": {"min_probability": 85, "title": "🟢 СИЛЬНЫЙ СИГНАЛ — НА СЕТ"},
        "standard": {"min_probability": 0, "title": "🎯 СИГНАЛ — НА СЕТ"},
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


@lru_cache(maxsize=1)
def get_signal_rules() -> dict[str, Any]:
    path = Path("signal_rules.yaml")
    if not path.exists():
        return DEFAULT_RULES
    with path.open("r", encoding="utf-8") as file:
        loaded = yaml.safe_load(file) or {}
    if not isinstance(loaded, dict):
        raise ValueError("signal_rules.yaml должен содержать YAML-словарь")
    return _deep_merge(DEFAULT_RULES, loaded)


def reload_signal_rules() -> dict[str, Any]:
    get_signal_rules.cache_clear()
    return get_signal_rules()
