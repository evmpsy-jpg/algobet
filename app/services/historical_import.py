from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.services.excel_parser import ParsedMatch, ParseResult
from app.services.import_service import to_utc_naive
from app.services.signal_results import infer_signal_result_status
from app.services.signal_rules import analyze_match


@dataclass(frozen=True)
class HistoricalSignalPreview:
    start_at: datetime
    send_at: datetime
    group: str
    level: str | None
    side: int | None
    player_1: str
    player_2: str
    score: str | None
    result_status: str | None


@dataclass(frozen=True)
class HistoricalImportPreview:
    total_rows: int
    parsed_matches: int
    selected_matches: int
    candidate_signals: int
    signals_by_group: dict[str, int]
    results_by_status: dict[str, int]
    without_result: int
    rejection_reasons: dict[str, int]
    first_signals: list[HistoricalSignalPreview] = field(default_factory=list)
    last_signals: list[HistoricalSignalPreview] = field(default_factory=list)


def filter_parse_result_by_period(
    result: ParseResult,
    *,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> ParseResult:
    matches: list[ParsedMatch] = []
    start_utc = to_utc_naive(start_at) if start_at is not None else None
    end_utc = to_utc_naive(end_at) if end_at is not None else None

    for match in result.matches:
        match_start = to_utc_naive(match.match_start_at)
        if start_utc is not None and match_start < start_utc:
            continue
        if end_utc is not None and match_start > end_utc:
            continue
        matches.append(match)

    return ParseResult(
        sheet_name=result.sheet_name,
        total_rows=result.total_rows,
        matches=matches,
        warnings=result.warnings,
    )


def _preview_item(match: ParsedMatch, send_at: datetime, payload: dict) -> HistoricalSignalPreview:
    side = payload.get("side")
    result_status = infer_signal_result_status(match.score, side if side in (1, 2) else None)
    group = str(payload.get("signal_group") or "unknown").strip().lower()
    if group not in {"vip", "all"}:
        group = "unknown"
    return HistoricalSignalPreview(
        start_at=to_utc_naive(match.match_start_at),
        send_at=send_at,
        group=group,
        level=payload.get("level"),
        side=side if side in (1, 2) else None,
        player_1=match.player_1,
        player_2=match.player_2,
        score=match.score,
        result_status=result_status,
    )


def preview_historical_import(result: ParseResult, *, signal_lead_minutes: int) -> HistoricalImportPreview:
    signals: list[HistoricalSignalPreview] = []
    signals_by_group: Counter[str] = Counter()
    results_by_status: Counter[str] = Counter()
    rejection_reasons: Counter[str] = Counter()
    without_result = 0

    for match in result.matches:
        decision = analyze_match(match)
        if not decision.suitable:
            rejection_reasons[decision.reason or "Матч не соответствует условиям"] += 1
            continue

        payload = decision.payload or {}
        send_at = to_utc_naive(match.match_start_at) - timedelta(minutes=signal_lead_minutes)
        item = _preview_item(match, send_at, payload)
        signals.append(item)
        signals_by_group[item.group] += 1
        if item.result_status is None:
            without_result += 1
        else:
            results_by_status[item.result_status] += 1

    signals.sort(key=lambda item: item.send_at)
    return HistoricalImportPreview(
        total_rows=result.total_rows,
        parsed_matches=len(result.matches),
        selected_matches=len(result.matches),
        candidate_signals=len(signals),
        signals_by_group=dict(signals_by_group),
        results_by_status=dict(results_by_status),
        without_result=without_result,
        rejection_reasons=dict(rejection_reasons),
        first_signals=signals[:5],
        last_signals=signals[-5:],
    )