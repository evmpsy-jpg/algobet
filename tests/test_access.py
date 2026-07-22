from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models import Base, User
from app.services.access import (
    disable_access,
    grant_paid_access,
    grant_trial_access,
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