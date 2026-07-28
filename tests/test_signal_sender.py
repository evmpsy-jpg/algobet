from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models import Base, Match, ScheduledSignal, SignalDelivery, User, UserAccess
from app.services.signal_sender import process_delivery_now, process_due_signals, process_signal_now


class FakeBot:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, *, chat_id: int, text: str, **_: object) -> None:
        if self.fail:
            raise RuntimeError("telegram unavailable")
        self.messages.append((chat_id, text))


async def make_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory


def make_match() -> Match:
    return Match(
        external_match_id=1001,
        external_tournament_id=2001,
        source_url="https://example.test/tournaments/2001/1001",
        tournament_date="21.07.2026",
        match_time="12:00",
        match_start_at=datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc),
        player_1="Игрок 1",
        player_2="Игрок 2",
        player_1_rating=None,
        player_2_rating=None,
        score=None,
        raw_data={},
    )


@pytest.mark.asyncio
async def test_process_due_signals_sends_to_trial_user_and_consumes_access() -> None:
    engine, factory = await make_session()
    async with factory() as session:
        user = User(telegram_id=111, username=None, first_name="Test", last_name=None)
        session.add(user)
        await session.flush()
        access = UserAccess(user_id=user.id, access_type="trial", status="active", free_signals_remaining=3)
        match = make_match()
        session.add_all([access, match])
        await session.flush()
        signal = ScheduledSignal(
            match_id=match.id,
            status="scheduled",
            send_at=datetime(2026, 7, 21, 11, 40, tzinfo=timezone.utc),
            message_text="signal text",
        )
        session.add(signal)
        await session.commit()

        bot = FakeBot()
        summary = await process_due_signals(
            bot,  # type: ignore[arg-type]
            session,
            now=datetime(2026, 7, 21, 11, 41, tzinfo=timezone.utc),
            admin_ids=[],
        )

        delivery = await session.scalar(select(SignalDelivery))
        assert summary.sent_deliveries == 1
        assert bot.messages == [(111, "signal text")]
        assert signal.status == "sent"
        assert access.free_signals_remaining == 2
        assert delivery is not None
        assert delivery.status == "sent"
    await engine.dispose()


@pytest.mark.asyncio
async def test_process_due_signals_skips_user_without_access() -> None:
    engine, factory = await make_session()
    async with factory() as session:
        user = User(telegram_id=222, username=None, first_name="No access", last_name=None)
        match = make_match()
        session.add_all([user, match])
        await session.flush()
        signal = ScheduledSignal(
            match_id=match.id,
            status="scheduled",
            send_at=datetime(2026, 7, 21, 11, 40, tzinfo=timezone.utc),
            message_text="signal text",
        )
        session.add(signal)
        await session.commit()

        bot = FakeBot()
        summary = await process_due_signals(
            bot,  # type: ignore[arg-type]
            session,
            now=datetime(2026, 7, 21, 11, 41, tzinfo=timezone.utc),
            admin_ids=[],
        )

        delivery = await session.scalar(select(SignalDelivery))
        assert summary.sent_deliveries == 0
        assert bot.messages == []
        assert signal.status == "ready"
        assert delivery is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_process_due_signals_records_failed_delivery() -> None:
    engine, factory = await make_session()
    async with factory() as session:
        user = User(telegram_id=333, username=None, first_name="Fail", last_name=None)
        session.add(user)
        await session.flush()
        access = UserAccess(user_id=user.id, access_type="trial", status="active", free_signals_remaining=3)
        match = make_match()
        session.add_all([access, match])
        await session.flush()
        signal = ScheduledSignal(
            match_id=match.id,
            status="scheduled",
            send_at=datetime(2026, 7, 21, 11, 40, tzinfo=timezone.utc),
            message_text="signal text",
        )
        session.add(signal)
        await session.commit()

        summary = await process_due_signals(
            FakeBot(fail=True),  # type: ignore[arg-type]
            session,
            now=datetime(2026, 7, 21, 11, 41, tzinfo=timezone.utc),
            admin_ids=[],
        )

        delivery = await session.scalar(select(SignalDelivery))
        assert summary.failed_deliveries == 1
        assert signal.status == "ready"
        assert access.free_signals_remaining == 3
        assert delivery is not None
        assert delivery.status == "failed"
        assert "telegram unavailable" in (delivery.error_text or "")
    await engine.dispose()

@pytest.mark.asyncio
async def test_process_due_signals_rechecks_trial_limit_between_signals() -> None:
    engine, factory = await make_session()
    async with factory() as session:
        user = User(telegram_id=444, username=None, first_name="One left", last_name=None)
        session.add(user)
        await session.flush()
        access = UserAccess(user_id=user.id, access_type="trial", status="active", free_signals_remaining=1)
        match_1 = make_match()
        match_2 = make_match()
        match_2.external_match_id = 1002
        session.add_all([access, match_1, match_2])
        await session.flush()
        session.add_all([
            ScheduledSignal(
                match_id=match_1.id,
                status="scheduled",
                send_at=datetime(2026, 7, 21, 11, 40, tzinfo=timezone.utc),
                message_text="signal one",
            ),
            ScheduledSignal(
                match_id=match_2.id,
                status="scheduled",
                send_at=datetime(2026, 7, 21, 11, 40, tzinfo=timezone.utc),
                message_text="signal two",
            ),
        ])
        await session.commit()

        bot = FakeBot()
        summary = await process_due_signals(
            bot,  # type: ignore[arg-type]
            session,
            now=datetime(2026, 7, 21, 11, 41, tzinfo=timezone.utc),
            admin_ids=[],
        )

        deliveries = list((await session.scalars(select(SignalDelivery))).all())
        assert summary.sent_deliveries == 1
        assert len(bot.messages) == 1
        assert access.free_signals_remaining == 0
        assert len(deliveries) == 1
    await engine.dispose()

@pytest.mark.asyncio
async def test_process_signal_now_sends_future_signal() -> None:
    engine, factory = await make_session()
    async with factory() as session:
        user = User(telegram_id=555, username=None, first_name="Manual", last_name=None)
        session.add(user)
        await session.flush()
        access = UserAccess(user_id=user.id, access_type="trial", status="active", free_signals_remaining=3)
        match = make_match()
        session.add_all([access, match])
        await session.flush()
        signal = ScheduledSignal(
            match_id=match.id,
            status="scheduled",
            send_at=datetime(2026, 7, 21, 13, 40, tzinfo=timezone.utc),
            message_text="future signal text",
        )
        session.add(signal)
        await session.commit()

        bot = FakeBot()
        due_summary = await process_due_signals(
            bot,  # type: ignore[arg-type]
            session,
            now=datetime(2026, 7, 21, 11, 41, tzinfo=timezone.utc),
            admin_ids=[],
        )
        manual_summary = await process_signal_now(
            bot,  # type: ignore[arg-type]
            session,
            signal.id,
            admin_ids=[],
        )

        delivery = await session.scalar(select(SignalDelivery))
        assert due_summary.processed_signals == 0
        assert manual_summary.processed_signals == 1
        assert manual_summary.sent_deliveries == 1
        assert bot.messages == [(555, "future signal text")]
        assert signal.status == "sent"
        assert access.free_signals_remaining == 2
        assert delivery is not None
        assert delivery.status == "sent"
    await engine.dispose()

@pytest.mark.asyncio
async def test_process_signal_now_retries_failed_without_duplicate_sent_delivery() -> None:
    engine, factory = await make_session()
    async with factory() as session:
        user_sent = User(telegram_id=666, username=None, first_name="Already", last_name=None)
        user_failed = User(telegram_id=777, username=None, first_name="Retry", last_name=None)
        session.add_all([user_sent, user_failed])
        await session.flush()
        access_sent = UserAccess(user_id=user_sent.id, access_type="trial", status="active", free_signals_remaining=3)
        access_failed = UserAccess(user_id=user_failed.id, access_type="trial", status="active", free_signals_remaining=3)
        match = make_match()
        session.add_all([access_sent, access_failed, match])
        await session.flush()
        signal = ScheduledSignal(
            match_id=match.id,
            status="sent",
            send_at=datetime(2026, 7, 21, 11, 40, tzinfo=timezone.utc),
            message_text="retry signal text",
        )
        session.add(signal)
        await session.flush()
        sent_delivery = SignalDelivery(
            signal_id=signal.id,
            user_id=user_sent.id,
            telegram_id=user_sent.telegram_id,
            status="sent",
            sent_at=datetime(2026, 7, 21, 11, 40, tzinfo=timezone.utc),
        )
        failed_delivery = SignalDelivery(
            signal_id=signal.id,
            user_id=user_failed.id,
            telegram_id=user_failed.telegram_id,
            status="failed",
            error_text="previous error",
        )
        session.add_all([sent_delivery, failed_delivery])
        await session.commit()

        bot = FakeBot()
        summary = await process_signal_now(
            bot,  # type: ignore[arg-type]
            session,
            signal.id,
            admin_ids=[],
        )

        deliveries = list((await session.scalars(select(SignalDelivery).order_by(SignalDelivery.telegram_id))).all())
        assert summary.processed_signals == 1
        assert summary.sent_deliveries == 1
        assert summary.skipped_users == 1
        assert bot.messages == [(777, "retry signal text")]
        assert signal.status == "sent"
        assert access_sent.free_signals_remaining == 3
        assert access_failed.free_signals_remaining == 2
        assert [delivery.status for delivery in deliveries] == ["sent", "sent"]
        assert deliveries[1].error_text is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_process_delivery_now_retries_selected_failed_delivery() -> None:
    engine, factory = await make_session()
    async with factory() as session:
        user = User(telegram_id=888, username=None, first_name="Retry", last_name=None)
        session.add(user)
        await session.flush()
        access = UserAccess(user_id=user.id, access_type="trial", status="active", free_signals_remaining=3)
        match = make_match()
        session.add_all([access, match])
        await session.flush()
        signal = ScheduledSignal(
            match_id=match.id,
            status="ready",
            send_at=datetime(2026, 7, 21, 11, 40, tzinfo=timezone.utc),
            signal_payload={},
            message_text="retry selected",
        )
        session.add(signal)
        await session.flush()
        delivery = SignalDelivery(
            signal_id=signal.id,
            user_id=user.id,
            telegram_id=user.telegram_id,
            status="failed",
            error_text="previous error",
        )
        session.add(delivery)
        await session.commit()

        bot = FakeBot()
        summary = await process_delivery_now(
            bot,  # type: ignore[arg-type]
            session,
            delivery.id,
            admin_ids=[],
        )

        saved = await session.get(SignalDelivery, delivery.id)

        assert summary.processed_signals == 1
        assert summary.sent_deliveries == 1
        assert bot.messages == [(888, "retry selected")]
        assert saved is not None
        assert saved.status == "sent"
        assert saved.error_text is None
        assert access.free_signals_remaining == 2
    await engine.dispose()

@pytest.mark.asyncio
async def test_process_due_signals_cancels_stale_signal_before_send() -> None:
    engine, factory = await make_session()
    async with factory() as session:
        user = User(telegram_id=999, username=None, first_name="Stale", last_name=None)
        session.add(user)
        await session.flush()
        access = UserAccess(user_id=user.id, access_type="trial", status="active", free_signals_remaining=3)
        match = make_match()
        match.raw_data = {
            "CP": 15,
            "DG": 7,
            "DH": 0,
            "EG": 2,
            "EH": 1,
            "CS": 6,
            "CT": 3,
            "CV": 75,
            "CW": 13,
            "Q": 5,
            "X": 4,
        }
        session.add_all([access, match])
        await session.flush()
        signal = ScheduledSignal(
            match_id=match.id,
            status="scheduled",
            send_at=datetime(2026, 7, 21, 11, 40, tzinfo=timezone.utc),
            signal_payload={"side": 2, "signal_group": "all", "probability": 76},
            message_text="stale signal text",
        )
        session.add(signal)
        await session.commit()

        bot = FakeBot()
        summary = await process_due_signals(
            bot,  # type: ignore[arg-type]
            session,
            now=datetime(2026, 7, 21, 11, 41, tzinfo=timezone.utc),
            admin_ids=[],
        )

        delivery = await session.scalar(select(SignalDelivery))
        assert summary.processed_signals == 1
        assert summary.sent_deliveries == 0
        assert bot.messages == []
        assert signal.status == "cancelled"
        assert signal.cancel_reason == "Актуальные данные матча больше не подходят под правила"
        assert access.free_signals_remaining == 3
        assert delivery is None
    await engine.dispose()