from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import SubscriptionRequest, User, UserAccess
from app.services.subscriptions import SubscriptionPlan

TRIAL_SIGNALS_LIMIT = 3
VIP_PROBABILITY_MIN = 99
ALL_SIGNALS_PROBABILITY_MIN = 95


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


def is_admin_user(user: User, admin_ids: list[int]) -> bool:
    return user.telegram_id in admin_ids


def _signal_probability(signal_payload: dict | None) -> float | None:
    if not isinstance(signal_payload, dict):
        return None
    value = signal_payload.get("probability", signal_payload.get("confidence"))
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _signal_group(signal_payload: dict | None) -> str | None:
    if not isinstance(signal_payload, dict):
        return None
    value = signal_payload.get("signal_group")
    if value is None:
        return None
    return str(value).strip().lower()


def _paid_access_is_active(access: UserAccess, *, now: datetime) -> bool:
    if access.status != "active":
        return False
    if access.active_until is not None and access.active_until < now:
        return False
    if access.signals_remaining is not None and access.signals_remaining <= 0:
        return False
    return True


def subscription_allows_signal(access: UserAccess, signal_payload: dict | None) -> bool:
    if access.access_type not in {"paid", "admin"}:
        return False
    if access.access_type == "admin":
        return True
    if not any([access.includes_vip, access.includes_all_signals, access.includes_analytics]):
        return True

    group = _signal_group(signal_payload)
    if group == "vip":
        return bool(access.includes_vip)
    if group == "all":
        return bool(access.includes_all_signals)

    probability = _signal_probability(signal_payload)
    if probability is None:
        return False
    if access.includes_all_signals and probability >= ALL_SIGNALS_PROBABILITY_MIN:
        return True
    if access.includes_vip and probability >= VIP_PROBABILITY_MIN:
        return True
    return False


def has_signal_access(
    user: User,
    access: UserAccess | None,
    *,
    admin_ids: list[int],
    now: datetime | None = None,
    signal_payload: dict | None = None,
) -> bool:
    if is_admin_user(user, admin_ids):
        return True
    if access is None or access.status != "active":
        return False
    now = now or datetime.utcnow()
    if access.access_type == "trial":
        return access.free_signals_remaining > 0
    if not _paid_access_is_active(access, now=now):
        return False
    if signal_payload is None:
        return True
    return subscription_allows_signal(access, signal_payload)


def has_analytics_access(
    user: User,
    access: UserAccess | None,
    *,
    admin_ids: list[int],
    now: datetime | None = None,
) -> bool:
    if is_admin_user(user, admin_ids):
        return True
    if access is None or access.access_type not in {"paid", "admin"}:
        return False
    now = now or datetime.utcnow()
    if not _paid_access_is_active(access, now=now):
        return False
    if access.access_type == "admin":
        return True
    if not any([access.includes_vip, access.includes_all_signals, access.includes_analytics]):
        return True
    return bool(access.includes_analytics)


def consume_signal_access(
    user: User,
    access: UserAccess | None,
    *,
    admin_ids: list[int],
    signal_payload: dict | None = None,
) -> None:
    if is_admin_user(user, admin_ids):
        return
    if access is None:
        return
    if access.access_type == "trial" and access.free_signals_remaining > 0:
        access.free_signals_remaining -= 1
        return
    if access.access_type == "paid" and access.signals_remaining is not None and subscription_allows_signal(access, signal_payload):
        access.signals_remaining = max(0, access.signals_remaining - 1)


async def grant_trial_access(session: AsyncSession, user: User) -> UserAccess:
    access = await session.scalar(select(UserAccess).where(UserAccess.user_id == user.id))
    if access is None:
        access = UserAccess(user_id=user.id)
        session.add(access)
        await session.flush()
    access.access_type = "trial"
    access.status = "active"
    access.free_signals_remaining = TRIAL_SIGNALS_LIMIT
    access.signals_remaining = None
    access.plan_id = None
    access.plan_group = None
    access.includes_vip = False
    access.includes_all_signals = False
    access.includes_analytics = False
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
    access.signals_remaining = None
    access.plan_id = None
    access.plan_group = None
    access.includes_vip = False
    access.includes_all_signals = False
    access.includes_analytics = False
    access.active_until = active_until
    return access


async def _get_or_create_access(session: AsyncSession, user: User) -> UserAccess:
    access = await session.scalar(select(UserAccess).where(UserAccess.user_id == user.id))
    if access is None:
        access = UserAccess(user_id=user.id)
        session.add(access)
        await session.flush()
    return access


def _apply_paid_subscription_fields(
    access: UserAccess,
    *,
    plan_id: str,
    plan_group: str,
    signals_limit: int | None,
    duration_hours: int | None,
    duration_days: int | None,
    includes_vip: bool,
    includes_all_signals: bool,
    includes_analytics: bool,
    now: datetime,
) -> UserAccess:
    active_until = None
    if duration_hours:
        active_until = now + timedelta(hours=duration_hours)
    elif duration_days:
        active_until = now + timedelta(days=duration_days)

    access.access_type = "paid"
    access.status = "active"
    access.free_signals_remaining = 0
    access.signals_remaining = signals_limit
    access.plan_id = plan_id
    access.plan_group = plan_group
    access.includes_vip = bool(includes_vip)
    access.includes_all_signals = bool(includes_all_signals)
    access.includes_analytics = bool(includes_analytics)
    access.active_until = active_until
    return access


async def grant_subscription_plan_access(
    session: AsyncSession,
    user: User,
    plan: SubscriptionPlan,
    *,
    now: datetime | None = None,
) -> UserAccess:
    access = await _get_or_create_access(session, user)
    return _apply_paid_subscription_fields(
        access,
        plan_id=plan.id,
        plan_group=plan.group,
        signals_limit=plan.signals_limit,
        duration_hours=plan.duration_hours,
        duration_days=plan.duration_days,
        includes_vip=plan.includes_vip,
        includes_all_signals=plan.includes_all_signals,
        includes_analytics=plan.includes_analytics,
        now=now or datetime.utcnow(),
    )


async def grant_subscription_access(
    session: AsyncSession,
    user: User,
    request: SubscriptionRequest,
    *,
    now: datetime | None = None,
) -> UserAccess:
    access = await _get_or_create_access(session, user)
    return _apply_paid_subscription_fields(
        access,
        plan_id=request.plan_id,
        plan_group=request.plan_group,
        signals_limit=request.signals_limit,
        duration_hours=request.duration_hours,
        duration_days=request.duration_days,
        includes_vip=request.includes_vip,
        includes_all_signals=request.includes_all_signals,
        includes_analytics=request.includes_analytics,
        now=now or datetime.utcnow(),
    )


async def disable_access(session: AsyncSession, user: User) -> UserAccess:
    access = await session.scalar(select(UserAccess).where(UserAccess.user_id == user.id))
    if access is None:
        access = UserAccess(user_id=user.id, access_type="trial", free_signals_remaining=0)
        session.add(access)
        await session.flush()
    access.status = "disabled"
    access.free_signals_remaining = 0
    access.signals_remaining = None
    access.active_until = None
    return access
