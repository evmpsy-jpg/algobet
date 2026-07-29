"""Пересчёт сигналов по текущим правилам.

Перебирает все матчи с сигналами в статусе scheduled/cancelled,
заново прогоняет evaluate_match по raw_data и обновляет БД.

Сигналы со статусом sent не трогает.

Запуск:
    python scripts/recalculate_signals.py [--dry-run] [--status scheduled,cancelled]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database.models import Match, ScheduledSignal
from app.database.session import SessionFactory, init_db
from app.services.match_normalizer import normalize_match
from app.services.rules_config import get_signal_rules
from app.services.signal_results import auto_set_signal_result
from app.services.signal_rules import build_signal_message
from app.signals.builder import build_signal
from app.signals.formatter import format_signal

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger(__name__)


def _parsed_match_from_match(match: Match):
    """Создаёт ParsedMatch-совместимый объект из ORM Match для normalize_match."""
    from app.services.excel_parser import ParsedMatch

    return ParsedMatch(
        external_match_id=match.external_match_id,
        external_tournament_id=match.external_tournament_id,
        source_url=match.source_url,
        tournament_date=match.tournament_date,
        tournament_name="",
        match_time=match.match_time,
        match_start_at=match.match_start_at,
        player_1=match.player_1,
        player_2=match.player_2,
        player_1_rating=match.player_1_rating,
        player_2_rating=match.player_2_rating,
        score=match.score,
        raw_data=match.raw_data or {},
    )


async def recalculate(dry_run: bool, statuses: list[str]) -> None:
    await init_db()
    rules = get_signal_rules()
    lead_minutes = int(rules["signal"].get("lead_minutes", 10))

    stats = {"checked": 0, "kept_scheduled": 0, "newly_scheduled": 0, "cancelled": 0, "skipped_sent": 0, "errors": 0}

    async with SessionFactory() as session:
        query = (
            select(ScheduledSignal)
            .join(Match, ScheduledSignal.match_id == Match.id)
            .where(ScheduledSignal.status.in_(statuses))
            .options(selectinload(ScheduledSignal.match))
            .order_by(Match.match_start_at)
        )
        signals = list((await session.scalars(query)).all())
        logger.info("Найдено сигналов для пересчёта: %d (статусы: %s)", len(signals), statuses)

        now_utc = datetime.utcnow()

        for signal in signals:
            match = signal.match
            stats["checked"] += 1
            try:
                parsed = _parsed_match_from_match(match)
                normalized = normalize_match(parsed)
                from app.rules.engine import evaluate_match
                decision = evaluate_match(normalized)
            except Exception as exc:
                logger.warning("Ошибка пересчёта match_id=%d: %s", match.external_match_id, exc)
                stats["errors"] += 1
                continue

            send_at = match.match_start_at.replace(tzinfo=None) - timedelta(minutes=lead_minutes)

            if not decision.suitable:
                reason = decision.reason or "Матч не соответствует условиям"
                if signal.status != "cancelled":
                    logger.info(
                        "ОТМЕНА  match_id=%d  %s vs %s  причина: %s",
                        match.external_match_id, match.player_1, match.player_2, reason,
                    )
                    if not dry_run:
                        signal.status = "cancelled"
                        signal.cancel_reason = reason
                        signal.recalculated_at = now_utc
                    stats["cancelled"] += 1
                else:
                    stats["kept_scheduled"] += 1
            else:
                message_text = build_signal_message(parsed, decision)
                if signal.status == "cancelled":
                    logger.info(
                        "ВОССТАНОВЛЕНИЕ  match_id=%d  %s vs %s  тип: %s",
                        match.external_match_id, match.player_1, match.player_2, decision.signal_type,
                    )
                    if not dry_run:
                        signal.status = "scheduled" if send_at > now_utc else signal.status
                        signal.signal_type = decision.signal_type
                        signal.signal_payload = decision.payload or {}
                        signal.message_text = message_text
                        signal.cancel_reason = None
                        signal.recalculated_at = now_utc
                    stats["newly_scheduled"] += 1
                else:
                    if not dry_run:
                        signal.signal_type = decision.signal_type
                        signal.signal_payload = decision.payload or {}
                        signal.message_text = message_text
                        signal.recalculated_at = now_utc
                        await auto_set_signal_result(session, signal, match)
                    stats["kept_scheduled"] += 1

        if not dry_run:
            await session.commit()
            logger.info("Изменения сохранены в БД.")
        else:
            logger.info("Режим --dry-run: изменения НЕ сохранены.")

    logger.info(
        "\nИтог:\n"
        "  Проверено:       %d\n"
        "  Оставлено:       %d\n"
        "  Восстановлено:   %d\n"
        "  Отменено:        %d\n"
        "  Ошибок:          %d",
        stats["checked"],
        stats["kept_scheduled"],
        stats["newly_scheduled"],
        stats["cancelled"],
        stats["errors"],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Пересчёт сигналов по текущим правилам.")
    parser.add_argument("--dry-run", action="store_true", help="Показать изменения без записи в БД.")
    parser.add_argument(
        "--status",
        default="scheduled,cancelled",
        help="Статусы для пересчёта через запятую (default: scheduled,cancelled).",
    )
    args = parser.parse_args()
    statuses = [s.strip() for s in args.status.split(",") if s.strip()]
    asyncio.run(recalculate(dry_run=args.dry_run, statuses=statuses))


if __name__ == "__main__":
    main()
