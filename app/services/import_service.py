from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.settings import get_settings
from app.database.models import ImportBatch, Match, MatchSnapshot, ScheduledSignal
from app.services.decision_log import record_decision_log
from app.services.excel_parser import ParseResult, parse_tournaments_file
from app.services.signal_rules import analyze_match, build_signal_message
from app.services.match_normalizer import normalize_match
from app.services.rules_config import get_signal_rules
from app.services.signal_results import auto_set_signal_result


def to_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


@dataclass(slots=True)
class ImportSummary:
    batch_id: int
    total_rows: int
    parsed_matches: int
    inserted_matches: int
    updated_matches: int
    missing_matches: int
    scheduled_signals: int
    cancelled_signals: int
    scheduled_by_group: dict[str, int]
    rejection_reasons: dict[str, int]
    warnings: list[str]


def calculate_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def calculate_text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def import_parse_result(
    session: AsyncSession,
    result: ParseResult,
    *,
    original_name: str,
    stored_path: str,
    file_hash: str,
    uploaded_by: int,
    mark_missing: bool = True,
    past_due_signal_action: str = "skip",
) -> ImportSummary:
    if past_due_signal_action not in {"skip", "store_sent"}:
        raise ValueError(f"Unsupported past_due_signal_action: {past_due_signal_action}")

    settings = get_settings()
    warnings = list(result.warnings)
    unique_matches = list({parsed.external_match_id: parsed for parsed in result.matches}.values())
    if len(unique_matches) != len(result.matches):
        duplicate_count = len(result.matches) - len(unique_matches)
        warnings.append(
            f"В файле найдены дубликаты external_match_id. Использована последняя запись, всего дубликатов: {duplicate_count}."
        )
    batch = ImportBatch(
        file_name=original_name,
        stored_path=stored_path,
        file_sha256=file_hash,
        uploaded_by_telegram_id=uploaded_by,
        status="processing",
        total_rows=result.total_rows,
        parsed_matches=len(unique_matches),
    )
    session.add(batch)
    await session.flush()

    inserted = 0
    updated_count = 0
    scheduled = 0
    cancelled = 0
    scheduled_by_group: Counter[str] = Counter()
    rejection_reasons: Counter[str] = Counter()

    if mark_missing:
        # Сначала отмечаем прошлые матчи отсутствующими. Найденные ниже вернём в актуальное состояние.
        await session.execute(
            update(Match)
            .where(Match.is_present_in_latest_import.is_(True))
            .values(is_present_in_latest_import=False)
        )

    for parsed in unique_matches:
        existing = await session.scalar(
            select(Match).where(Match.external_match_id == parsed.external_match_id)
        )
        if existing is None:
            existing = Match(
                external_match_id=parsed.external_match_id,
                external_tournament_id=parsed.external_tournament_id,
                source_url=parsed.source_url,
                tournament_date=parsed.tournament_date,
                match_time=parsed.match_time,
                match_start_at=to_utc_naive(parsed.match_start_at),
                player_1=parsed.player_1,
                player_2=parsed.player_2,
                player_1_rating=parsed.player_1_rating,
                player_2_rating=parsed.player_2_rating,
                score=parsed.score,
                raw_data=parsed.raw_data,
                current_import_id=batch.id,
                is_present_in_latest_import=True,
            )
            session.add(existing)
            await session.flush()
            inserted += 1
        else:
            existing.external_tournament_id = parsed.external_tournament_id
            existing.source_url = parsed.source_url
            existing.tournament_date = parsed.tournament_date
            existing.match_time = parsed.match_time
            existing.match_start_at = to_utc_naive(parsed.match_start_at)
            existing.player_1 = parsed.player_1
            existing.player_2 = parsed.player_2
            existing.player_1_rating = parsed.player_1_rating
            existing.player_2_rating = parsed.player_2_rating
            existing.score = parsed.score
            existing.raw_data = parsed.raw_data
            existing.current_import_id = batch.id
            existing.is_present_in_latest_import = True
            existing.last_seen_at = datetime.utcnow()
            updated_count += 1

        normalized = normalize_match(parsed)

        session.add(
            MatchSnapshot(
                import_batch_id=batch.id,
                external_match_id=parsed.external_match_id,
                data={
                    "players": [parsed.player_1, parsed.player_2],
                    "ratings": [parsed.player_1_rating, parsed.player_2_rating],
                    "date": parsed.tournament_date,
                    "time": parsed.match_time,
                    "start_at": parsed.match_start_at.isoformat(),
                    "score": parsed.score,
                    "raw_data": parsed.raw_data,
                    "normalized": {
                        "h2h_games": normalized.h2h_games,
                        "probability_p1": normalized.probability_p1,
                        "probability_p2": normalized.probability_p2,
                        "set1_handicap": normalized.set1_handicap,
                        "set2_handicap": normalized.set2_handicap,
                        "set3_handicap": normalized.set3_handicap,
                    },
                },
            )
        )

        decision = analyze_match(parsed)
        await record_decision_log(
            session,
            match_id=existing.id,
            decision=decision,
            import_batch_id=batch.id,
            source="import",
        )
        signal = await session.scalar(
            select(ScheduledSignal).where(ScheduledSignal.match_id == existing.id)
        )

        if signal is not None and signal.status == "sent":
            await auto_set_signal_result(session, signal, existing)

        if decision.suitable:
            lead_minutes = int(get_signal_rules()["signal"].get("lead_minutes", settings.signal_lead_minutes))
            send_at = to_utc_naive(parsed.match_start_at) - timedelta(minutes=lead_minutes)
            now_utc = datetime.utcnow()

            if signal is not None and signal.status == "sent":
                continue

            if send_at <= now_utc:
                reason = "Время отправки сигнала уже прошло"
                rejection_reasons[reason] += 1
                if past_due_signal_action == "store_sent":
                    group = str((decision.payload or {}).get("signal_group") or "unknown").strip().lower()
                    scheduled_by_group[group if group in {"vip", "all"} else "unknown"] += 1
                    if signal is None:
                        signal = ScheduledSignal(match_id=existing.id, send_at=send_at)
                        session.add(signal)
                        await session.flush()
                    signal.status = "sent"
                    signal.send_at = send_at
                    signal.sent_at = send_at
                    signal.signal_type = decision.signal_type
                    signal.signal_payload = decision.payload or {}
                    signal.message_text = build_signal_message(parsed, decision)
                    signal.source_import_id = batch.id
                    signal.cancel_reason = None
                    signal.recalculated_at = now_utc
                    scheduled += 1
                    await auto_set_signal_result(session, signal, existing)
                    continue
                if signal is not None and signal.status != "sent":
                    signal.status = "cancelled"
                    signal.cancel_reason = reason
                    signal.source_import_id = batch.id
                    signal.recalculated_at = now_utc
                    cancelled += 1
                continue

            group = str((decision.payload or {}).get("signal_group") or "unknown").strip().lower()
            scheduled_by_group[group if group in {"vip", "all"} else "unknown"] += 1
            if signal is None:
                signal = ScheduledSignal(match_id=existing.id, send_at=send_at)
                session.add(signal)
            signal.status = "scheduled"
            signal.send_at = send_at
            signal.signal_type = decision.signal_type
            signal.signal_payload = decision.payload or {}
            signal.message_text = build_signal_message(parsed, decision)
            signal.source_import_id = batch.id
            signal.cancel_reason = None
            signal.recalculated_at = now_utc
            scheduled += 1
        elif signal is not None and signal.status != "sent":
            rejection_reasons[decision.reason or "Матч не соответствует условиям"] += 1
            signal.status = "cancelled"
            signal.cancel_reason = decision.reason or "Матч не соответствует условиям"
            signal.source_import_id = batch.id
            signal.recalculated_at = datetime.utcnow()
            cancelled += 1
        elif not decision.suitable:
            rejection_reasons[decision.reason or "Матч не соответствует условиям"] += 1

    missing_matches = []
    if mark_missing:
        missing_query = select(Match).where(Match.is_present_in_latest_import.is_(False))
        missing_matches = list((await session.scalars(missing_query)).all())
    for missing in missing_matches:
        signal = await session.scalar(
            select(ScheduledSignal).where(ScheduledSignal.match_id == missing.id)
        )
        if signal is not None and signal.status != "sent":
            signal.status = "cancelled"
            signal.cancel_reason = "Матч отсутствует в последней загруженной таблице"
            signal.source_import_id = batch.id
            cancelled += 1
            rejection_reasons[signal.cancel_reason] += 1

    batch.status = "completed"
    batch.inserted_matches = inserted
    batch.updated_matches = updated_count
    batch.missing_matches = len(missing_matches)
    batch.error_text = "\n".join(warnings) if warnings else None
    batch.finished_at = datetime.utcnow()
    await session.commit()

    return ImportSummary(
        batch_id=batch.id,
        total_rows=result.total_rows,
        parsed_matches=len(unique_matches),
        inserted_matches=inserted,
        updated_matches=updated_count,
        missing_matches=len(missing_matches),
        scheduled_signals=scheduled,
        cancelled_signals=cancelled,
        scheduled_by_group=dict(scheduled_by_group),
        rejection_reasons=dict(rejection_reasons),
        warnings=warnings,
    )


async def import_tournaments(
    session: AsyncSession,
    file_path: Path,
    original_name: str,
    uploaded_by: int,
    mark_missing: bool = True,
    past_due_signal_action: str = "skip",
) -> ImportSummary:
    if past_due_signal_action not in {"skip", "store_sent"}:
        raise ValueError(f"Unsupported past_due_signal_action: {past_due_signal_action}")

    settings = get_settings()
    file_hash = calculate_sha256(file_path)
    result = parse_tournaments_file(file_path, settings.timezone)
    return await import_parse_result(
        session,
        result,
        original_name=original_name,
        stored_path=str(file_path),
        file_hash=file_hash,
        uploaded_by=uploaded_by,
        mark_missing=mark_missing,
        past_due_signal_action=past_due_signal_action,
    )
