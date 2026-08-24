from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
import app.services.import_service as import_service
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models import Base, ImportBatch, Match, MatchSnapshot, ScheduledSignal, SignalDecisionLog, SignalResult
from app.services.excel_parser import ParsedMatch, ParseResult
from app.services.import_service import import_parse_result, prune_match_snapshots


class FrozenDateTime(datetime):
    @classmethod
    def utcnow(cls) -> datetime:
        return datetime(2026, 7, 27, 12, 0)


@pytest.mark.asyncio
async def test_import_parse_result_does_not_schedule_past_due_signal(monkeypatch) -> None:
    monkeypatch.setattr(import_service, "datetime", FrozenDateTime)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    parsed = ParsedMatch(
        external_match_id=1001,
        external_tournament_id=2001,
        source_url="https://example.test/tournaments/2001/1001",
        tournament_date="27.07.2026",
        tournament_name="Тест",
        match_time="14:00",
        match_start_at=datetime(2026, 7, 27, 14, 0, tzinfo=ZoneInfo("Europe/Moscow")),
        player_1="Игрок 1",
        player_2="Игрок 2",
        player_1_rating=None,
        player_2_rating=None,
        score=None,
        raw_data={
            "CP": 5,
            "Q": 7,
            "X": 7,
            "CV": 90,
            "CW": 10,
            "DG": 8,
            "DH": 0,
            "EG": 0,
            "EH": 0,
            "CS": 0,
            "CT": 0,
        },
    )
    result = ParseResult(sheet_name="Лист", total_rows=1, matches=[parsed], warnings=[])

    async with factory() as session:
        summary = await import_parse_result(
            session,
            result,
            original_name="google",
            stored_path="google-sheets://test",
            file_hash="hash",
            uploaded_by=1,
            mark_missing=False,
        )
        scheduled_count = int(await session.scalar(select(func.count(ScheduledSignal.id))) or 0)

    assert summary.scheduled_signals == 0
    assert scheduled_count == 0
    assert summary.rejection_reasons["Время отправки сигнала уже прошло"] == 1

    await engine.dispose()

@pytest.mark.asyncio
async def test_import_parse_result_can_store_past_due_signal_for_history(monkeypatch) -> None:
    monkeypatch.setattr(import_service, "datetime", FrozenDateTime)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    parsed = ParsedMatch(
        external_match_id=1002,
        external_tournament_id=2002,
        source_url="https://example.test/tournaments/2002/1002",
        tournament_date="27.07.2026",
        tournament_name="Тест",
        match_time="14:00",
        match_start_at=datetime(2026, 7, 27, 14, 0, tzinfo=ZoneInfo("Europe/Moscow")),
        player_1="Игрок 1",
        player_2="Игрок 2",
        player_1_rating=None,
        player_2_rating=None,
        score="1:3",
        raw_data={
            "CP": 5,
            "Q": 7,
            "X": 7,
            "CV": 90,
            "CW": 10,
            "DG": 8,
            "DH": 0,
            "EG": 0,
            "EH": 0,
            "CS": 0,
            "CT": 0,
        },
    )
    result = ParseResult(sheet_name="Лист", total_rows=1, matches=[parsed], warnings=[])

    async with factory() as session:
        summary = await import_parse_result(
            session,
            result,
            original_name="google historical",
            stored_path="google-sheets://test?historical=1",
            file_hash="hash-history",
            uploaded_by=1,
            mark_missing=False,
            past_due_signal_action="store_sent",
        )
        signal = await session.scalar(select(ScheduledSignal))
        signal_result = await session.scalar(select(SignalResult))

    assert summary.scheduled_signals == 1
    assert signal is not None
    assert signal.status == "sent"
    assert signal.sent_at == signal.send_at
    assert signal_result is not None
    assert signal_result.status == "won"
    assert signal_result.source == "auto"

    await engine.dispose()

@pytest.mark.asyncio
async def test_import_parse_result_deduplicates_same_external_match_id(monkeypatch) -> None:
    monkeypatch.setattr(import_service, "datetime", FrozenDateTime)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    first = ParsedMatch(
        external_match_id=2001,
        external_tournament_id=3001,
        source_url="https://example.test/tournaments/3001/2001",
        tournament_date="27.07.2026",
        tournament_name="Тест",
        match_time="14:00",
        match_start_at=datetime(2030, 7, 27, 14, 0, tzinfo=ZoneInfo("Europe/Moscow")),
        player_1="Игрок A",
        player_2="Игрок B",
        player_1_rating=None,
        player_2_rating=None,
        score=None,
        raw_data={
            "CP": 5,
            "Q": 7,
            "X": 7,
            "CV": 90,
            "CW": 10,
            "DG": 8,
            "DH": 0,
            "EG": 0,
            "EH": 0,
            "CS": 0,
            "CT": 0,
        },
    )
    second = ParsedMatch(
        external_match_id=2001,
        external_tournament_id=3001,
        source_url="https://example.test/tournaments/3001/2001",
        tournament_date="27.07.2026",
        tournament_name="Тест",
        match_time="14:00",
        match_start_at=datetime(2030, 7, 27, 14, 0, tzinfo=ZoneInfo("Europe/Moscow")),
        player_1="Игрок A2",
        player_2="Игрок B2",
        player_1_rating=None,
        player_2_rating=None,
        score="3:1",
        raw_data={
            "CP": 5,
            "Q": 7,
            "X": 7,
            "CV": 90,
            "CW": 10,
            "DG": 8,
            "DH": 0,
            "EG": 0,
            "EH": 0,
            "CS": 0,
            "CT": 0,
        },
    )
    result = ParseResult(sheet_name="Лист", total_rows=2, matches=[first, second], warnings=[])

    async with factory() as session:
        summary = await import_parse_result(
            session,
            result,
            original_name="google duplicate",
            stored_path="google-sheets://test/duplicate",
            file_hash="hash-duplicate",
            uploaded_by=1,
            mark_missing=False,
            past_due_signal_action="store_sent",
        )
        match = await session.scalar(select(Match))
        snapshot_count = int(await session.scalar(select(func.count(MatchSnapshot.id))) or 0)
        log_count = int(await session.scalar(select(func.count(SignalDecisionLog.id))) or 0)
        signal_count = int(await session.scalar(select(func.count(ScheduledSignal.id))) or 0)

    assert summary.parsed_matches == 1
    assert summary.scheduled_signals == 1
    assert match is not None
    assert match.player_1 == "Игрок A2"
    assert snapshot_count == 1
    assert log_count == 1
    assert signal_count == 1

    await engine.dispose()

@pytest.mark.asyncio
async def test_prune_match_snapshots_keeps_latest_imports_only() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        for index in range(12):
            batch = ImportBatch(
                file_name=f"batch-{index}",
                stored_path=f"batch-{index}",
                file_sha256=f"hash-{index}",
                uploaded_by_telegram_id=1,
                status="completed",
            )
            session.add(batch)
            await session.flush()
            session.add(MatchSnapshot(import_batch_id=batch.id, external_match_id=index, data={"index": index}))
        await session.commit()

        await prune_match_snapshots(session, keep_imports=10)
        await session.commit()

        snapshot_batches = list((await session.scalars(select(MatchSnapshot.import_batch_id).order_by(MatchSnapshot.import_batch_id))).all())

    await engine.dispose()

    assert len(snapshot_batches) == 10
    assert snapshot_batches == list(range(3, 13))
