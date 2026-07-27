from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models import Base, Match, ScheduledSignal, SignalDecisionLog, SignalDelivery, User
from app.services.access import grant_trial_access
import app.services.import_service as import_service
from app.services.import_service import import_tournaments, to_utc_naive
from app.services.signal_sender import process_due_signals


class FrozenDateTime(datetime):
    @classmethod
    def utcnow(cls) -> datetime:
        return datetime(2026, 7, 19, 0, 0)

class FakeBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, *, chat_id: int, text: str, **_: object) -> None:
        self.messages.append((chat_id, text))


def test_to_utc_naive_converts_moscow_match_time_for_scheduler() -> None:
    match_start = datetime(2026, 7, 25, 12, 30, tzinfo=ZoneInfo("Europe/Moscow"))

    assert to_utc_naive(match_start) == datetime(2026, 7, 25, 9, 30)


@pytest.mark.asyncio
async def test_excel_import_creates_signals_and_delivers_trial_messages(monkeypatch) -> None:
    monkeypatch.setattr(import_service, "datetime", FrozenDateTime)
    source = Path("tests/sample.xlsx")
    assert source.exists()

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        user = User(
            telegram_id=777000,
            username="integration_user",
            first_name="Integration",
            last_name="User",
        )
        session.add(user)
        await session.flush()
        access = await grant_trial_access(session, user)

        summary = await import_tournaments(
            session,
            source,
            source.name,
            uploaded_by=315715137,
        )

        match_count = int(await session.scalar(select(func.count(Match.id))) or 0)
        decision_log_count = int(await session.scalar(select(func.count(SignalDecisionLog.id))) or 0)
        scheduled_count = int(await session.scalar(select(func.count(ScheduledSignal.id))) or 0)

        assert summary.parsed_matches == 168
        assert summary.scheduled_signals == 20
        assert sum(summary.scheduled_by_group.values()) == summary.scheduled_signals
        assert summary.scheduled_by_group.get("vip", 0) + summary.scheduled_by_group.get("all", 0) == summary.scheduled_signals
        assert match_count == summary.parsed_matches
        assert decision_log_count == summary.parsed_matches
        assert scheduled_count == summary.scheduled_signals

        bot = FakeBot()
        sender_summary = await process_due_signals(
            bot,  # type: ignore[arg-type]
            session,
            now=datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc),
            admin_ids=[],
        )

        deliveries = list((await session.scalars(select(SignalDelivery))).all())
        sent_signals = int(await session.scalar(select(func.count(ScheduledSignal.id)).where(ScheduledSignal.status == "sent")) or 0)
        ready_signals = int(await session.scalar(select(func.count(ScheduledSignal.id)).where(ScheduledSignal.status == "ready")) or 0)

        assert sender_summary.processed_signals == summary.scheduled_signals
        assert sender_summary.sent_deliveries == 3
        assert len(bot.messages) == 3
        assert len(deliveries) == 3
        assert all(delivery.status == "sent" for delivery in deliveries)
        assert sent_signals == 3
        assert ready_signals == 17
        assert access.free_signals_remaining == 0
        assert all("СИГНАЛ" in message for _, message in bot.messages)

    await engine.dispose()
