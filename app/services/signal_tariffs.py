from __future__ import annotations

from typing import Any

VIP_PROBABILITY_MIN = 99
ALL_SIGNALS_PROBABILITY_MIN = 95

SIGNAL_TARIFF_ORDER = ("vip_99", "all_95", "unknown")


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


def signal_group(signal_payload: dict[str, Any] | None) -> str | None:
    if not isinstance(signal_payload, dict):
        return None
    value = signal_payload.get("signal_group")
    if value is None:
        return None
    normalized = str(value).strip().lower()
    return normalized if normalized in {"vip", "all"} else None


def signal_tariff_key(signal_payload: dict[str, Any] | None) -> str:
    group = signal_group(signal_payload)
    if group == "vip":
        return "vip_99"
    if group == "all":
        return "all_95"
    return "unknown"


def signal_tariff_title(key: str) -> str:
    return {
        "vip_99": "VIP 99%",
        "all_95": "Все остальные сигналы",
        "unknown": "Без типа",
    }.get(key.lower(), key.upper())
