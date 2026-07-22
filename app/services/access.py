from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User, UserAccess

TRIAL_SIGNALS_LIMIT = 3


def is_admin_user(user: User, admin_ids: list[int]) -> bool:
    return user.telegram_id in admin_ids


async def ensure_trial_access(session: AsyncSession, user: User) -> UserAccess:
    access = await session.scalar(select(UserAccess).where(UserAccess.user_id == user.id))
    if access is None:
        access = UserAccess(
            user_id=user.id,
            access_type="trial",
            status="active",
            free_signals_remaining=TRIAL_SIGNALS_LIMIT,
        )
        session.add(access)
        await session.flush()
    return access


def has_signal_access(
    user: User,
    access: UserAccess | None,
    *,
    admin_ids: list[int],
    now: datetime | None = None,
) -> bool:
    if is_admin_user(user, admin_ids):
        return True
    if access is None or access.status != "active":
        return False
    now = now or datetime.utcnow()
    if access.active_until is not None and access.active_until < now:
        return False
    if access.access_type == "trial":
        return access.free_signals_remaining > 0
    return access.access_type in {"paid", "admin"}


def consume_signal_access(user: User, access: UserAccess | None, *, admin_ids: list[int]) -> None:
    if is_admin_user(user, admin_ids):
        return
    if access is not None and access.access_type == "trial" and access.free_signals_remaining > 0:
        access.free_signals_remaining -= 1

async def grant_trial_access(session: AsyncSession, user: User) -> UserAccess:
    access = await session.scalar(select(UserAccess).where(UserAccess.user_id == user.id))
    if access is None:
        access = UserAccess(user_id=user.id)
        session.add(access)
        await session.flush()
    access.access_type = "trial"
    access.status = "active"
    access.free_signals_remaining = TRIAL_SIGNALS_LIMIT
    access.active_until = None
    return access


async def grant_paid_access(
    session: AsyncSession,
    user: User,
    *,
    active_until: datetime | None = None,
) -> UserAccess:
    access = await session.scalar(select(UserAccess).where(UserAccess.user_id == user.id))
    if access is None:
        access = UserAccess(user_id=user.id)
        session.add(access)
        await session.flush()
    access.access_type = "paid"
    access.status = "active"
    access.free_signals_remaining = 0
    access.active_until = active_until
    return access


async def disable_access(session: AsyncSession, user: User) -> UserAccess:
    access = await session.scalar(select(UserAccess).where(UserAccess.user_id == user.id))
    if access is None:
        access = UserAccess(user_id=user.id, access_type="trial", free_signals_remaining=0)
        session.add(access)
        await session.flush()
    access.status = "disabled"
    access.free_signals_remaining = 0
    access.active_until = None
    return access
