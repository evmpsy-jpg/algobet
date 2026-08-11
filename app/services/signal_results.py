from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.excel_mapping import get_excel_mapping
from app.database.models import Match, ScheduledSignal, SignalResult
from app.domain.models import MatchData
from app.rules.engine import evaluate_match
from app.services.signal_tariffs import signal_tariff_key

RESULT_STATUSES = {"won", "lost", "void", "unknown"}
RESULT_LABELS = {
    "won": "✅ Зашёл",
    "lost": "❌ Не зашёл",
    "void": "↩️ Возврат",
    "unknown": "❔ Неизвестно",
}
RESULT_SHORT_LABELS = {
    "won": "✅",
    "lost": "❌",
    "void": "↩️",
    "unknown": "❔",
}
LEVEL_ORDER = ("TOP", "STRONG", "STANDARD")
SIGNAL_GROUP_ORDER = ("vip", "all", "unknown")
SCORE_PAIR_RE = re.compile(r"(\d+)\s*[:\-–—]\s*(\d+)")


@dataclass
class ResultCounter:
    won: int = 0
    lost: int = 0
    void: int = 0
    unknown: int = 0

    @property
    def resolved(self) -> int:
        return self.won + self.lost

    @property
    def total(self) -> int:
        return self.won + self.lost + self.void + self.unknown

    @property
    def winrate(self) -> float | None:
        if self.resolved == 0:
            return None
        return self.won / self.resolved * 100

    def add(self, status: str) -> None:
        if status == "won":
            self.won += 1
        elif status == "lost":
            self.lost += 1
        elif status == "void":
            self.void += 1
        else:
            self.unknown += 1


@dataclass
class AutoResultSummary:
    scanned: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped_manual: int = 0
    no_score: int = 0
    updated_items: list[str] = field(default_factory=list)


@dataclass
class ResultSummary:
    total_sent: int = 0
    overall: ResultCounter = field(default_factory=ResultCounter)
    by_group: dict[str, ResultCounter] = field(default_factory=dict)
    by_tariff: dict[str, ResultCounter] = field(default_factory=dict)
    by_level: dict[str, ResultCounter] = field(default_factory=dict)

    @property
    def evaluated(self) -> int:
        return self.overall.total

    @property
    def unrated_sent(self) -> int:
        return max(0, self.total_sent - self.evaluated)


def result_label(status: str | None) -> str:
    return RESULT_LABELS.get(status or "unknown", RESULT_LABELS["unknown"])


def result_short_label(status: str | None) -> str:
    return RESULT_SHORT_LABELS.get(status or "unknown", RESULT_SHORT_LABELS["unknown"])


def result_source_label(source: str | None) -> str:
    if source == "auto":
        return "авто"
    if source == "manual":
        return "вручную"
    return "не задан"


def result_full_label(result: SignalResult | None) -> str:
    if result is None:
        return result_label(None)
    return f"{result_label(result.status)} · {result_source_label(result.source)}"


def format_auto_result_item(signal: ScheduledSignal, match: Match, status: str) -> str:
    side = signal.signal_payload.get("side") if signal.signal_payload else None
    side_text = f"П{side}" if side in (1, 2) else "—"
    score = match.score or "—"
    return f"{result_short_label(status)} {side_text} · {match.player_1} — {match.player_2} · {score}"


def infer_signal_result_status(score: str | None, side: int | None) -> str | None:
    if side not in (1, 2) or not score:
        return None
    normalized = score.strip()
    if not normalized or normalized in {"-:-", "-", "—"}:
        return None

    pairs = [(int(left), int(right)) for left, right in SCORE_PAIR_RE.findall(normalized)]
    if not pairs:
        return None

    if len(pairs) == 1:
        left, right = pairs[0]
        if left == right:
            return "void"
        selected_sets = left if side == 1 else right
        return "won" if selected_sets > 0 else "lost"

    p1_sets = sum(1 for left, right in pairs if left > right)
    p2_sets = sum(1 for left, right in pairs if right > left)
    if p1_sets == 0 and p2_sets == 0:
        return "void"
    selected_sets = p1_sets if side == 1 else p2_sets
    return "won" if selected_sets > 0 else "lost"



async def auto_set_signal_result(
    session: AsyncSession,
    signal: ScheduledSignal,
    match: Match,
) -> SignalResult | None:
    side = signal.signal_payload.get("side") if signal.signal_payload else None
    status = infer_signal_result_status(match.score, side)
    result = await session.scalar(select(SignalResult).where(SignalResult.signal_id == signal.id))
    if status is None:
        if result is not None and result.source == "auto":
            await session.delete(result)
        return None

    now = datetime.utcnow()
    if result is not None and result.source != "auto":
        return None
    if result is None:
        result = SignalResult(signal_id=signal.id, status=status, source="auto")
        session.add(result)
    else:
        result.status = status
        result.source = "auto"
    result.fixed_at = now
    result.updated_at = now
    return result


async def auto_update_signal_results(session: AsyncSession) -> AutoResultSummary:
    summary = AutoResultSummary()
    rows = list((await session.execute(
        select(ScheduledSignal, Match, SignalResult)
        .join(Match, Match.id == ScheduledSignal.match_id)
        .outerjoin(SignalResult, SignalResult.signal_id == ScheduledSignal.id)
        .where(ScheduledSignal.status == "sent")
    )).all())
    now = datetime.utcnow()
    for signal, match, result in rows:
        summary.scanned += 1
        side = signal.signal_payload.get("side") if signal.signal_payload else None
        status = infer_signal_result_status(match.score, side)
        if status is None:
            summary.no_score += 1
            if result is not None and result.source == "auto":
                await session.delete(result)
                summary.updated += 1
            continue
        if result is not None and result.source != "auto":
            summary.skipped_manual += 1
            continue
        if result is None:
            result = SignalResult(signal_id=signal.id, status=status, source="auto")
            result.fixed_at = now
            result.updated_at = now
            session.add(result)
            summary.updated += 1
            summary.updated_items.append(format_auto_result_item(signal, match, status))
            continue
        if result.status == status and result.source == "auto":
            summary.unchanged += 1
            continue
        result.status = status
        result.source = "auto"
        result.fixed_at = now
        result.updated_at = now
        summary.updated += 1
        summary.updated_items.append(format_auto_result_item(signal, match, status))
    return summary


async def set_signal_result(
    session: AsyncSession,
    signal: ScheduledSignal,
    status: str,
    fixed_by_telegram_id: int | None,
) -> SignalResult:
    if status not in RESULT_STATUSES:
        raise ValueError(f"Unsupported signal result status: {status}")

    result = await session.scalar(select(SignalResult).where(SignalResult.signal_id == signal.id))
    now = datetime.utcnow()
    if result is None:
        result = SignalResult(signal_id=signal.id, status=status)
        session.add(result)
    else:
        result.status = status
    result.source = "manual"
    result.fixed_by_telegram_id = fixed_by_telegram_id
    result.fixed_at = now
    result.updated_at = now
    await session.commit()
    await session.refresh(result)
    return result


def signal_group_key(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return "unknown"
    group = str(payload.get("signal_group") or "unknown").strip().lower()
    return group if group in {"vip", "all"} else "unknown"


def signal_group_title(group: str) -> str:
    return {
        "vip": "VIP",
        "all": "Все сигналы",
        "unknown": "Без типа",
    }.get(group.lower(), group.upper())




def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace("%", "").replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def _match_data_for_stats(match: Match) -> MatchData | None:
    raw_data = match.raw_data if isinstance(match.raw_data, dict) else None
    if raw_data is None:
        return None
    columns = get_excel_mapping()

    def get(field: str) -> float | None:
        return _number(raw_data.get(columns[field]))

    return MatchData(
        match_id=match.external_match_id or match.id,
        tournament_id=match.external_tournament_id or 0,
        source_url=match.source_url,
        tournament_date=match.tournament_date,
        tournament_name=str(raw_data.get("_tournament_name") or ""),
        match_time=match.match_time,
        match_start_at=match.match_start_at,
        player_1=match.player_1,
        player_2=match.player_2,
        player_1_rating=match.player_1_rating,
        player_2_rating=match.player_2_rating,
        score=match.score,
        h2h_games=get("h2h_games"),
        form_p1=get("form_p1"),
        form_p2=get("form_p2"),
        favorite_form_p1=get("favorite_form_p1"),
        favorite_form_p2=get("favorite_form_p2"),
        bg_p1=get("bg_p1"),
        bf_p2=get("bf_p2"),
        probability_p1=get("probability_p1"),
        probability_p2=get("probability_p2"),
        all_signal_p1=get("all_signal_p1"),
        all_signal_p2=get("all_signal_p2"),
        p1_exact=get("p1_exact"),
        p2_exact=get("p2_exact"),
        p1_range=get("p1_range"),
        p2_range=get("p2_range"),
        signal_balance=get("signal_balance"),
        h2h_p1=get("h2h_p1"),
        h2h_p2=get("h2h_p2"),
        average_h2h_handicap=get("average_h2h_handicap"),
        average_difference=get("average_difference"),
        set1_handicap=get("set1_handicap"),
        set2_handicap=get("set2_handicap"),
        set3_handicap=get("set3_handicap"),
        raw_data=raw_data,
    )


def signal_stats_cp_value(raw_data: dict[str, Any] | None) -> float | None:
    if not isinstance(raw_data, dict):
        return None
    value = raw_data.get("CP")
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def signal_stats_eligible(match_or_raw_data: Match | dict[str, Any] | None, *, min_cp: float = 5.0, decision_suitable: bool | None = None) -> bool:
    if decision_suitable is False:
        return False

    raw_data: dict[str, Any] | None
    if isinstance(match_or_raw_data, Match):
        raw_data = match_or_raw_data.raw_data if isinstance(match_or_raw_data.raw_data, dict) else None
        if raw_data is None:
            return False
        columns = get_excel_mapping()
        rule_keys = (
            columns["form_p1"],
            columns["form_p2"],
            columns["probability_p1"],
            columns["probability_p2"],
            columns["all_signal_p1"],
            columns["all_signal_p2"],
            columns["p1_exact"],
            columns["p2_exact"],
            columns["p1_range"],
            columns["p2_range"],
            columns["signal_balance"],
        )
        if any(raw_data.get(key) is not None for key in rule_keys):
            match_data = _match_data_for_stats(match_or_raw_data)
            if match_data is None:
                return False
            if not evaluate_match(match_data).suitable:
                return False
    else:
        raw_data = match_or_raw_data if isinstance(match_or_raw_data, dict) else None

    cp_value = signal_stats_cp_value(raw_data)
    return cp_value is not None and cp_value >= min_cp


def summarize_results(rows: list[tuple[dict[str, Any] | None, str | None]], total_sent: int = 0) -> ResultSummary:
    summary = ResultSummary(total_sent=total_sent)
    for payload, status in rows:
        normalized_status = status if status in RESULT_STATUSES else "unknown"
        level = "—"
        if isinstance(payload, dict):
            level = str(payload.get("level") or "—")
        group = signal_group_key(payload)
        tariff = signal_tariff_key(payload)
        summary.overall.add(normalized_status)
        if group not in summary.by_group:
            summary.by_group[group] = ResultCounter()
        summary.by_group[group].add(normalized_status)
        if tariff not in summary.by_tariff:
            summary.by_tariff[tariff] = ResultCounter()
        summary.by_tariff[tariff].add(normalized_status)
        if level not in summary.by_level:
            summary.by_level[level] = ResultCounter()
        summary.by_level[level].add(normalized_status)
    return summary


def format_winrate(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}%"
