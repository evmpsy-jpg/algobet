from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models import Base, SubscriptionRequest, User
from app.services.access import (
    consume_signal_access,
    disable_access,
    grant_paid_access,
    grant_subscription_access,
    grant_trial_access,
    has_analytics_access,
    has_signal_access,
)


async def make_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory


@pytest.mark.asyncio
async def test_grant_trial_access_resets_free_signal_limit() -> None:
    engine, factory = await make_session()
    async with factory() as session:
        user = User(telegram_id=111, username=None, first_name="Trial", last_name=None)
        session.add(user)
        await session.flush()

        access = await grant_trial_access(session, user)
        access.free_signals_remaining = 0
        access = await grant_trial_access(session, user)

        assert access.access_type == "trial"
        assert access.status == "active"
        assert access.free_signals_remaining == 3
        assert has_signal_access(user, access, admin_ids=[]) is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_grant_paid_access_enables_access_without_trial_limit() -> None:
    engine, factory = await make_session()
    async with factory() as session:
        user = User(telegram_id=222, username=None, first_name="Paid", last_name=None)
        session.add(user)
        await session.flush()

        access = await grant_paid_access(session, user)

        assert access.access_type == "paid"
        assert access.status == "active"
        assert access.free_signals_remaining == 0
        assert has_signal_access(user, access, admin_ids=[]) is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_disable_access_blocks_non_admin_user() -> None:
    engine, factory = await make_session()
    async with factory() as session:
        user = User(telegram_id=333, username=None, first_name="Disabled", last_name=None)
        session.add(user)
        await session.flush()

        access = await grant_paid_access(session, user)
        access = await disable_access(session, user)

        assert access.status == "disabled"
        assert has_signal_access(user, access, admin_ids=[]) is False
    await engine.dispose()


def test_admin_user_has_access_without_access_row() -> None:
    user = User(telegram_id=444, username=None, first_name="Admin", last_name=None)

    assert has_signal_access(user, None, admin_ids=[444]) is True

@pytest.mark.asyncio
async def test_subscription_access_filters_vip_and_all_signal_groups() -> None:
    engine, factory = await make_session()
    async with factory() as session:
        user = User(telegram_id=555, username=None, first_name="VIP", last_name=None)
        session.add(user)
        await session.flush()
        request = SubscriptionRequest(
            user_id=user.id,
            telegram_id=user.telegram_id,
            plan_id="vip_10",
            plan_group="vip",
            plan_title="VIP 99%",
            plan_description="10 сигналов",
            price_rub=2500,
            signals_limit=10,
            includes_vip=True,
        )

        access = await grant_subscription_access(session, user, request)

        assert has_signal_access(user, access, admin_ids=[], signal_payload={"signal_group": "vip"}) is True
        assert has_signal_access(user, access, admin_ids=[], signal_payload={"signal_group": "all"}) is False
        assert has_signal_access(user, access, admin_ids=[], signal_payload={"probability": 99}) is True
        assert has_signal_access(user, access, admin_ids=[], signal_payload={"probability": 98}) is False

        request.plan_id = "all_10"
        request.plan_group = "all"
        request.plan_title = "Все сигналы 95%"
        request.includes_vip = False
        request.includes_all_signals = True
        access = await grant_subscription_access(session, user, request)

        assert has_signal_access(user, access, admin_ids=[], signal_payload={"signal_group": "all"}) is True
        assert has_signal_access(user, access, admin_ids=[], signal_payload={"signal_group": "vip"}) is False
        assert has_signal_access(user, access, admin_ids=[], signal_payload={"probability": 95}) is True
        assert has_signal_access(user, access, admin_ids=[], signal_payload={"probability": 94}) is False
    await engine.dispose()


@pytest.mark.asyncio
async def test_subscription_access_consumes_limited_signal_package() -> None:
    engine, factory = await make_session()
    async with factory() as session:
        user = User(telegram_id=666, username=None, first_name="Limited", last_name=None)
        session.add(user)
        await session.flush()
        request = SubscriptionRequest(
            user_id=user.id,
            telegram_id=user.telegram_id,
            plan_id="all_10",
            plan_group="all",
            plan_title="Все сигналы 95%",
            plan_description="10 сигналов",
            price_rub=1500,
            signals_limit=1,
            includes_all_signals=True,
        )
        access = await grant_subscription_access(session, user, request)

        assert has_signal_access(user, access, admin_ids=[], signal_payload={"signal_group": "all"}) is True
        consume_signal_access(user, access, admin_ids=[], signal_payload={"signal_group": "all"})

        assert access.signals_remaining == 0
        assert has_signal_access(user, access, admin_ids=[], signal_payload={"signal_group": "all"}) is False
    await engine.dispose()


@pytest.mark.asyncio
async def test_included_subscription_grants_analytics_access() -> None:
    engine, factory = await make_session()
    async with factory() as session:
        user = User(telegram_id=777, username=None, first_name="Analytics", last_name=None)
        session.add(user)
        await session.flush()
        request = SubscriptionRequest(
            user_id=user.id,
            telegram_id=user.telegram_id,
            plan_id="included_48h",
            plan_group="included",
            plan_title="Всё включено",
            plan_description="48 часов",
            price_rub=4500,
            duration_hours=48,
            includes_vip=True,
            includes_all_signals=True,
            includes_analytics=True,
        )
        access = await grant_subscription_access(session, user, request)

        assert has_analytics_access(user, access, admin_ids=[]) is True
        assert has_signal_access(user, access, admin_ids=[], signal_payload={"signal_group": "all"}) is True
        assert has_signal_access(user, access, admin_ids=[], signal_payload={"signal_group": "vip"}) is True
    await engine.dispose()
