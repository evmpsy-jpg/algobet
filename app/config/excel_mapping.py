from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


DEFAULT_MAPPING: dict[str, str] = {
    "h2h_games": "CP",
    "form_p1": "Q",
    "form_p2": "X",
    "bg_p1": "BG",
    "bf_p2": "BF",
    "probability_p1": "CV",
    "probability_p2": "CW",
    "p1_exact": "EG",
    "p2_exact": "EH",
    "p1_range": "CS",
    "p2_range": "CT",
    "h2h_p1": "AA",
    "h2h_p2": "AB",
    "average_h2h_handicap": "AI",
    "average_difference": "EF",
    "set1_handicap": "DS",
    "set2_handicap": "DV",
    "set3_handicap": "DY",
}


@lru_cache(maxsize=1)
def get_excel_mapping() -> dict[str, str]:
    path = Path("excel_mapping.yaml")
    if not path.exists():
        return dict(DEFAULT_MAPPING)
    with path.open("r", encoding="utf-8") as file:
        loaded: Any = yaml.safe_load(file) or {}
    mapping = loaded.get("columns", loaded) if isinstance(loaded, dict) else {}
    if not isinstance(mapping, dict):
        raise ValueError("excel_mapping.yaml должен содержать раздел columns")
    result = dict(DEFAULT_MAPPING)
    result.update({str(k): str(v) for k, v in mapping.items()})
    return result


def reload_excel_mapping() -> dict[str, str]:
    get_excel_mapping.cache_clear()
    return get_excel_mapping()
