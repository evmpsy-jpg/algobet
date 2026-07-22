import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models import Base, SubscriptionRequest, User
from app.services.subscriptions import (
    create_subscription_request,
    format_price,
    format_subscription_plans_text,
    format_subscription_request_admin_text,
    format_subscription_request_user_text,
    get_subscription_plan,
)


async def make_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory


def make_user() -> User:
    return User(
        telegram_id=111,
        username="tester",
        first_name="Тест",
        last_name="Игрок",
    )


def test_subscription_plans_include_new_pricing() -> None:
    vip = get_subscription_plan("vip_100")
    included = get_subscription_plan("included_30d")

    assert vip is not None
    assert vip.title == "VIP 99%"
    assert vip.signals_limit == 100
    assert vip.price_rub == 20000
    assert included is not None
    assert included.includes_vip is True
    assert included.includes_all_signals is True
    assert included.includes_analytics is True
    assert included.duration_days == 30
    assert included.price_rub == 25000


def test_format_subscription_plans_text_lists_all_groups() -> None:
    text = format_subscription_plans_text()

    assert "VIP 99%" in text
    assert "Все сигналы 95%" in text
    assert "Всё включено" in text
    assert "25 000р" in text


@pytest.mark.asyncio
async def test_create_subscription_request_persists_selected_plan() -> None:
    engine, factory = await make_session()
    async with factory() as session:
        user = make_user()
        session.add(user)
        await session.flush()

        request = await create_subscription_request(session, user, "all_50")
        await session.commit()

        saved = await session.scalar(select(SubscriptionRequest))
        assert saved is not None
        assert request.id == saved.id
        assert saved.status == "new"
        assert saved.plan_title == "Все сигналы 95%"
        assert saved.signals_limit == 50
        assert saved.price_rub == 7000
    await engine.dispose()


def test_format_subscription_request_texts_contain_plan_and_price() -> None:
    user = make_user()
    request = SubscriptionRequest(
        id=5,
        user_id=1,
        telegram_id=111,
        username="tester",
        plan_id="vip_10",
        plan_group="vip",
        plan_title="VIP 99%",
        plan_description="10 сигналов",
        price_rub=2500,
        signals_limit=10,
        payment_details="Карта 0000",
        specialist_contact="@spec",
    )

    assert format_price(2500) == "2 500р"
    user_text = format_subscription_request_user_text(request)
    admin_text = format_subscription_request_admin_text(request, user)

    assert "#5" in user_text
    assert "VIP 99%" in user_text
    assert "2 500р" in user_text
    assert "@spec" in user_text
    assert "Новая заявка" in admin_text
    assert "@tester" in admin_text
