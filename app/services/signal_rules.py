"""Совместимый фасад Stage 5.1.

Старые обработчики продолжают импортировать analyze_match/build_signal_message,
но бизнес-логика теперь находится в domain -> rules -> signals.
"""
from __future__ import annotations

from app.domain.models import SignalDecision
from app.rules.engine import evaluate_match
from app.services.excel_parser import ParsedMatch
from app.services.match_normalizer import normalize_match
from app.signals.builder import build_signal
from app.signals.formatter import format_signal


def analyze_match(match: ParsedMatch) -> SignalDecision:
    normalized = normalize_match(match)
    return evaluate_match(normalized)


def build_signal_message(match: ParsedMatch, decision: SignalDecision) -> str:
    normalized = normalize_match(match)
    signal = build_signal(normalized, decision)
    return format_signal(signal)
