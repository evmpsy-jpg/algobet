from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.import_service as import_service
from app.database.models import Base, ScheduledSignal
from app.services.excel_parser import ParsedMatch, ParseResult
from app.services.import_service import import_parse_result


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