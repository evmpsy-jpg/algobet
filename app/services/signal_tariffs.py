from __future__ import annotations

from typing import Any

VIP_PROBABILITY_MIN = 99
ALL_SIGNALS_PROBABILITY_MIN = 95

SIGNAL_TARIFF_ORDER = ("vip_99", "all_95", "below_95", "unknown")


def signal_probability(signal_payload: dict[str, Any] | None) -> float | None:
    if not isinstance(signal_payload, dict):
        return None
    value = signal_payload.get("probability", signal_payload.get("confidence"))
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def signal_tariff_key(signal_payload: dict[str, Any] | None) -> str:
    probability = signal_probability(signal_payload)
    if probability is None:
        return "unknown"
    if probability >= VIP_PROBABILITY_MIN:
        return "vip_99"
    if probability >= ALL_SIGNALS_PROBABILITY_MIN:
        return "all_95"
    return "below_95"


def signal_tariff_title(key: str) -> str:
    return {
        "vip_99": "VIP 99%",
        "all_95": "Все сигналы 95%",
        "below_95": "Ниже 95%",
        "unknown": "Без вероятности",
    }.get(key.lower(), key.upper())
